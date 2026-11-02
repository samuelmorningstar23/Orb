"""Installed RAG fixture safety checks; these tests never load ML models."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from capacity_retrieval_fixture import (
    RetrievalCounters,
    prepare_full_retrieval,
    retrieval_integrity,
    snapshot_proof,
)
from loadtest_capacity import parser, startup_diagnostic, validate_safety


def snapshot(path: Path, *, reranker: bool = False, dimension: int = 1024) -> str:
    path.mkdir()
    (path / "config.json").write_text(json.dumps({"hidden_size": dimension, "private_extra": "NEVER-PERSIST",
        "architectures": ["XLMRobertaForSequenceClassification" if reranker else "XLMRobertaModel"]}))
    return str(path)


def test_snapshot_proof_requires_production_dimensions_and_hides_paths(tmp_path: Path) -> None:
    path = snapshot(tmp_path / "embedding")
    proof = snapshot_proof(path)
    assert proof["hidden_size"] == 1024 and proof["expected_model"] == "BAAI/bge-m3"
    assert str(tmp_path) not in json.dumps(proof) and "NEVER-PERSIST" not in json.dumps(proof)
    with pytest.raises(ValueError, match="1024"):
        snapshot_proof(snapshot(tmp_path / "small", dimension=384))
    with pytest.raises(ValueError, match="1024"):
        snapshot_proof(path, reranker=True)


@pytest.mark.parametrize("path", ["BAAI/bge-m3", "https://example.com/model", "relative/snapshot"])
def test_hub_ids_urls_and_relative_snapshots_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="installed absolute"):
        snapshot_proof(path)


def test_installed_retrieval_paths_are_paired_and_owned_fixture_only(tmp_path: Path) -> None:
    embedding = snapshot(tmp_path / "embedding")
    reranker = snapshot(tmp_path / "reranker", reranker=True)
    args = parser().parse_args(["--orb-stub", "--embedding-model", embedding])
    with pytest.raises(ValueError, match="both installed"):
        validate_safety(args, None)
    args = parser().parse_args(["--synthetic", "--embedding-model", embedding, "--reranker-model", reranker])
    with pytest.raises(ValueError, match="owned Orb"):
        validate_safety(args, None)
    args = parser().parse_args(["--orb-stub", "--embedding-model", embedding, "--reranker-model", reranker])
    validate_safety(args, None)


def test_full_retrieval_preparation_refuses_an_unowned_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="owned disposable"):
        prepare_full_retrieval("unused", "unused")


def test_worker_counters_preserve_errors_and_never_store_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    services = ModuleType("services")
    services.__path__ = []
    knowledge = ModuleType("services.clinical_knowledge")
    def embed(*args: Any, **kwargs: Any) -> list[bytes]:
        return [b"x"]
    def rerank(*args: Any, **kwargs: Any) -> list[float]:
        raise RuntimeError("NEVER-PERSIST-ERROR")
    def results(*args: Any, **kwargs: Any) -> list[dict[str, str]]:
        return [{"rerank_method": "cross_encoder"}, {"rerank_method": "lexical"}, {"rerank_method": "SECRET-UNKNOWN"}]
    knowledge.embed_texts_isolated = embed
    knowledge.rerank_scores_isolated = rerank
    knowledge.rerank_results = results
    services.clinical_knowledge = knowledge
    monkeypatch.setitem(sys.modules, "services", services)
    monkeypatch.setitem(sys.modules, "services.clinical_knowledge", knowledge)
    counters = RetrievalCounters()
    counters.install()
    assert knowledge.embed_texts_isolated(["NEVER-PERSIST-QUERY"]) == [b"x"]
    with pytest.raises(RuntimeError, match="NEVER-PERSIST-ERROR"):
        knowledge.rerank_scores_isolated("NEVER-PERSIST-QUERY", [])
    knowledge.rerank_results("NEVER-PERSIST-QUERY", [])
    report = counters.report()
    assert report["embedding_completed"] == 1 and report["rerank_failed"] == 1
    assert report["results_cross_encoder"] == report["results_lexical"] == report["results_other"] == 1
    assert "NEVER-PERSIST" not in json.dumps(report) and "SECRET" not in json.dumps(report)


def test_fast_lexical_degradation_cannot_qualify_full_retrieval() -> None:
    proof = {"nonzero_embedding_probe": True, "cross_encoder_probe": True}
    counts = {"embedding_completed": 2, "rerank_completed": 2}
    assert retrieval_integrity(proof, counts)["passed"]
    assert not retrieval_integrity(proof, {**counts, "results_lexical": 1})["passed"]
    assert not retrieval_integrity(proof, {**counts, "embedding_failed": 1})["passed"]
    assert not retrieval_integrity(proof, {})["passed"]


def test_startup_diagnostic_never_outputs_raw_worker_error_or_paths(tmp_path: Path) -> None:
    (tmp_path / "capacity-startup-stage.json").write_text(json.dumps({"stage": "embedding_probe",
        "exception_type": "RuntimeError", "message": "SECRET-QUERY", "path": str(tmp_path)}))
    assert startup_diagnostic(tmp_path) == "stage=embedding_probe; exception=RuntimeError"
    (tmp_path / "capacity-startup-stage.json").write_text(json.dumps({"stage": "SECRET-PATIENT", "exception_type": "SECRET_CLASS"}))
    assert startup_diagnostic(tmp_path) == "stage=unknown; exception=unknown"
