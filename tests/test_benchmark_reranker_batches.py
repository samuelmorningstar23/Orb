"""Batch candidates must retain exact inputs, score positions and thresholds."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from benchmark_reranker_batches import batch_plan, compare_scores, padding_work, prepare_inputs, public_workload, scatter_scores


@pytest.mark.parametrize("size", [8, 16, 32])
def test_bucket_scatter_preserves_every_original_pair_position(size: int) -> None:
    lengths = [512, 1, 192, 65, 300, 12, 48, 1, 200, 9, 400, 2, 32, 201, 500, 40, 90, 60, 33]
    batches = batch_plan(lengths, size, bucket=True)
    assert sorted(index for batch in batches for index in batch) == list(range(len(lengths)))
    outputs = [[index / 100 for index in batch] for batch in batches]
    assert scatter_scores(batches, outputs, len(lengths)) == [index / 100 for index in range(len(lengths))]
    assert all(len(batch) <= size for batch in batches)
    assert padding_work(lengths, batches)["real_tokens"] == sum(lengths)


@pytest.mark.parametrize("batches,outputs,count", [
    ([[0]], [], 1), ([[0, 1]], [[.1]], 2), ([[0, 0]], [[.1, .2]], 2),
    ([[0]], [[.1]], 2), ([[0]], [[float("nan")]], 1), ([[2]], [[.1]], 1),
])
def test_missing_duplicate_nonfinite_or_misrouted_scores_fail_closed(
    batches: list[list[int]], outputs: list[list[float]], count: int,
) -> None:
    with pytest.raises(ValueError):
        scatter_scores(batches, outputs, count)


def test_small_score_change_cannot_hide_a_relevance_threshold_flip() -> None:
    result = compare_scores([[.9, .31542, .1]], [[.9, .31540, .1]], .31541)
    assert result["max_absolute_difference"] < result["score_tolerance"]
    assert result["threshold_decision_changes"] == 1 and not result["quality_equivalence_passed"]


def test_changed_top3_order_fails_even_when_all_threshold_decisions_match() -> None:
    result = compare_scores([[.9, .8, .7001, .7]], [[.9, .8, .7, .7001]], .31541)
    assert result["threshold_decision_changes"] == 0 and result["top3_order_changes"] == 1
    assert not result["quality_equivalence_passed"]


def test_unchanged_scores_and_stable_ties_pass() -> None:
    result = compare_scores([[.9, .8, .7, .7]], [[.9, .8, .7, .7]], .31541)
    assert result["quality_equivalence_passed"]


class FakeBackend:
    def __init__(self) -> None:
        self.truncation: dict[str, Any] | None = {"max_length": 100, "strategy": "longest_first"}

    def enable_truncation(self, **settings: Any) -> None:
        self.truncation = dict(settings)

    def no_truncation(self) -> None:
        self.truncation = None

    def post_process(self, query: Any, passage: Any, **kwargs: Any) -> Any:
        assert self.truncation == {"max_length": 512, "strategy": "only_second"}
        ids = [101] + query.ids + [102] + passage.ids[:512 - len(query.ids) - 3] + [102]
        return SimpleNamespace(ids=ids, attention_mask=[1] * len(ids), type_ids=[0] * len(ids))


class FakeTokenizer:
    model_input_names = ["input_ids", "attention_mask"]

    def __init__(self) -> None:
        self.backend_tokenizer = FakeBackend()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, texts: list[str], **settings: Any) -> Any:
        self.calls.append(settings)
        return SimpleNamespace(encodings=[SimpleNamespace(ids=list(range(min(len(text), settings["max_length"])))) for text in texts])


def test_preparation_keeps_query_passage_caps_and_restores_tokenizer_state() -> None:
    tokenizer = FakeTokenizer()
    scorer = SimpleNamespace(tokenizer=tokenizer)
    rows = prepare_inputs(scorer, [["q" * 900, "p" * 1200], ["short", "passage"]])
    assert tokenizer.calls == [
        {"add_special_tokens": False, "truncation": True, "max_length": 384},
        {"add_special_tokens": False, "truncation": True, "max_length": 512},
    ]
    assert len(rows[0]["input_ids"]) == 512 and len(rows[0]["attention_mask"]) == 512
    assert tokenizer.backend_tokenizer.truncation == {"max_length": 100, "strategy": "longest_first"}
    assert "token_type_ids" not in rows[0]


def test_public_corpus_loader_never_executes_source_code(tmp_path: Path) -> None:
    services = tmp_path / "services"
    services.mkdir()
    marker = tmp_path / "must-not-exist"
    (services / "clinical_knowledge.py").write_text(f"CLINICAL_DOCUMENTS = __import__('pathlib').Path({str(marker)!r}).touch()")
    with pytest.raises(ValueError):
        public_workload(tmp_path)
    assert not marker.exists()


def test_public_workload_reads_only_literal_corpus_and_static_calibration(tmp_path: Path) -> None:
    (tmp_path / "services").mkdir()
    (tmp_path / "eval").mkdir()
    rows = [{"document_id": f"SYNTH-{index}", "title": "Synthetic", "content": "Static test passage"} for index in range(19)]
    (tmp_path / "services/clinical_knowledge.py").write_text("CLINICAL_DOCUMENTS = " + repr(rows))
    (tmp_path / "eval/retrieval_calibration.json").write_text(json.dumps({"cases": [{"query": "Synthetic question", "expected": ["SYNTH-1"]}]}))
    (tmp_path / "eval/retrieval_thresholds.json").write_text(json.dumps({"min_relevance_cross_encoder": .31541}))
    documents, cases, floor = public_workload(tmp_path)
    assert len(documents) == 19 and len(cases) == 2 and floor == .31541
    assert cases[-1]["id"] == "BOUNDARY-QUERY"
