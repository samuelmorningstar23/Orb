"""Qualification gates reject token, vector and retrieval ordering drift."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

SOURCE = Path(__file__).resolve().parents[1] / "scripts/qualify_embedding_microbatch.py"
SPEC = importlib.util.spec_from_file_location("qualify_embedding_microbatch", SOURCE)
assert SPEC and SPEC.loader
qualifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualifier)


def arrays():
    source = np.zeros((2, 1024), dtype=np.float32)
    source[0, 0] = 1
    source[1, 1] = 1
    corpus = np.zeros((6, 1024), dtype=np.float32)
    corpus[:, 0] = [1, .9, .8, .7, .6, .5]
    corpus[:, 1] = [.5, .6, .7, .8, .9, 1]
    return source, source.copy(), corpus


def test_exact_embeddings_and_prepared_inputs_pass():
    old, new, corpus = arrays()
    assert qualifier.compare(old, new, corpus, ["a", "b"], ["a", "b"])["passed"]


def test_prepared_token_change_fails_even_with_identical_vectors():
    old, new, corpus = arrays()
    report = qualifier.compare(old, new, corpus, ["a", "b"], ["a", "changed"])
    assert not report["passed"]
    assert not report["checks"]["actual_unpadded_ids_and_masks_equal"]


def test_missing_prepared_input_capture_fails():
    old, new, corpus = arrays()
    assert not qualifier.compare(old, new, corpus, [], [])["passed"]


def test_vector_or_retrieval_rank_drift_fails():
    old, new, corpus = arrays()
    new[0, :2] = [0, 1]
    report = qualifier.compare(old, new, corpus, ["a", "b"], ["a", "b"])
    assert not report["passed"]
    assert report["retrieval_top5_order_changes"] == 1


def test_public_synthetic_workload_has_long_short_and_unique_queries():
    passages, queries = qualifier.synthetic_workload()
    assert len(queries) == 24 and len(set(queries)) == 24
    assert len(passages) >= 19
    assert all(passage.startswith("Title: ") and "\nContent: " in passage for passage in passages)
    assert min(map(len, queries)) < 100
    assert max(map(len, queries)) > 4000
