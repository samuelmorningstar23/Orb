"""Script-only unchanged-BGE batching experiment over public seeded corpus.

Each configuration uses an owned copied-source child. No database or Orb app is
opened. Scores are compared with production batch8, not asserted clinically
correct. Models must already be installed; downloads and remote code are off.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

from capacity_retrieval_fixture import snapshot_proof
from e2e_server import isolated_environment, local_snapshot_path
from qualify_ollama_serving import pressure_abort_reason, vm_counters
from serve_ollama_candidate import GIB, inspect_memory

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("services/local_transformers.py", "services/clinical_knowledge.py",
           "eval/retrieval_calibration.json", "eval/retrieval_thresholds.json")


def batch_plan(lengths: list[int], batch_size: int, *, bucket: bool = False) -> list[list[int]]:
    if batch_size not in {8, 16, 32} or any(type(length) is not int or length < 1 or length > 512 for length in lengths):
        raise ValueError("Invalid batch size or prepared token lengths")
    indices = sorted(range(len(lengths)), key=lambda index: (lengths[index], index)) if bucket else list(range(len(lengths)))
    return [indices[start:start + batch_size] for start in range(0, len(indices), batch_size)]


def scatter_scores(batches: list[list[int]], outputs: list[list[float]], count: int) -> list[float]:
    result: list[float | None] = [None] * count
    if len(batches) != len(outputs):
        raise ValueError("Missing model batch")
    for indices, scores in zip(batches, outputs, strict=True):
        if len(indices) != len(scores):
            raise ValueError("Missing model scores")
        for index, score in zip(indices, scores, strict=True):
            if not 0 <= index < count or result[index] is not None or not math.isfinite(score):
                raise ValueError("Duplicate/misrouted/nonfinite model score")
            result[index] = score
    if any(value is None for value in result):
        raise ValueError("Dropped model score")
    return [float(value) for value in result if value is not None]


def padding_work(lengths: list[int], batches: list[list[int]]) -> dict[str, int]:
    return {"real_tokens": sum(lengths),
            "padded_tokens": sum(max(lengths[index] for index in batch) * len(batch) for batch in batches)}


def compare_scores(reference: list[list[float]], candidate: list[list[float]], floor: float,
                   *, tolerance: float = .005) -> dict[str, Any]:
    if len(reference) != len(candidate) or not reference:
        raise ValueError("Missing query score rows")
    differences: list[float] = []
    top3_changes = threshold_changes = 0
    def rank(values: list[float]) -> list[int]:
        return sorted(range(len(values)), key=lambda index: (-values[index], index))[:3]
    for old, new in zip(reference, candidate, strict=True):
        if len(old) != len(new) or not old or not all(math.isfinite(value) for value in old + new):
            raise ValueError("Missing or nonfinite candidate scores")
        differences.extend(abs(a - b) for a, b in zip(old, new, strict=True))
        top3_changes += rank(old) != rank(new)
        threshold_changes += sum((a >= floor) != (b >= floor) for a, b in zip(old, new, strict=True))
    maximum = max(differences)
    return {"pairs": len(differences), "max_absolute_difference": maximum,
            "mean_absolute_difference": statistics.fmean(differences), "score_tolerance": tolerance,
            "top3_order_changes": top3_changes, "relevance_threshold": floor,
            "threshold_decision_changes": threshold_changes,
            "quality_equivalence_passed": maximum <= tolerance and top3_changes == 0 and threshold_changes == 0}


def public_workload(server: Path) -> tuple[list[dict[str, str]], list[dict[str, Any]], float]:
    tree = ast.parse((server / "services/clinical_knowledge.py").read_text())
    documents = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "CLINICAL_DOCUMENTS" for target in node.targets):
            documents = ast.literal_eval(node.value)
            break
    if not isinstance(documents, list) or len(documents) != 19:
        raise ValueError("Expected the current19 public/synthetic seeded corpus documents")
    safe_documents = []
    for row in documents:
        if not isinstance(row, dict) or not all(isinstance(row.get(name), str) for name in ("document_id", "title", "content")):
            raise ValueError("Malformed public corpus entry")
        safe_documents.append({"id": row["document_id"], "passage": row["title"] + "\n" + row["content"]})
    raw = json.loads((server / "eval/retrieval_calibration.json").read_text())["cases"]
    cases = []
    for index, row in enumerate(raw):
        query = row.get("query")
        if not isinstance(query, str) or not 1 <= len(query) <= 4096:
            raise ValueError("Malformed static retrieval calibration query")
        cases.append({"id": f"CAL-{index:03d}", "query": query, "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                      "expected": row.get("expected", []), "relevant": row.get("relevant")})
    if not 1 <= len(cases) <= 100:
        raise ValueError("Unbounded calibration workload")
    boundary_query = "synthetic repeated clinical boundary " * 80 + "sepsis"
    cases.append({"id": "BOUNDARY-QUERY", "query": boundary_query,
                  "query_sha256": hashlib.sha256(boundary_query.encode()).hexdigest(), "expected": [], "relevant": None})
    floor = json.loads((server / "eval/retrieval_thresholds.json").read_text())["min_relevance_cross_encoder"]
    return safe_documents, cases, float(floor)


def prepare_inputs(reranker: Any, pairs: list[list[str]]) -> list[dict[str, list[int]]]:
    """Exact production token boundaries, without changing tokenizer state."""
    tokenizer = reranker.tokenizer
    queries = tokenizer([pair[0] for pair in pairs], add_special_tokens=False, truncation=True, max_length=384)
    passages = tokenizer([pair[1] for pair in pairs], add_special_tokens=False, truncation=True, max_length=512)
    backend = tokenizer.backend_tokenizer
    previous = backend.truncation
    try:
        backend.enable_truncation(max_length=512, strategy="only_second")
        encodings = [backend.post_process(query, passage, add_special_tokens=True)
                     for query, passage in zip(queries.encodings, passages.encodings, strict=True)]
    finally:
        if previous is None:
            backend.no_truncation()
        else:
            backend.enable_truncation(**previous)
    return [{"input_ids": encoding.ids, "attention_mask": encoding.attention_mask,
             **({"token_type_ids": encoding.type_ids} if "token_type_ids" in tokenizer.model_input_names else {})}
            for encoding in encodings]


def input_hash(row: dict[str, list[int]]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def synchronize(torch: Any, device: Any) -> None:
    if device.type == "mps":
        torch.mps.synchronize()
    elif device.type == "cuda":
        torch.cuda.synchronize()


def device_memory(torch: Any, device: Any) -> dict[str, int]:
    if device.type == "mps":
        return {"mps_current_allocated_bytes": torch.mps.current_allocated_memory(),
                "mps_driver_allocated_bytes": torch.mps.driver_allocated_memory()}
    if device.type == "cuda":
        return {"cuda_current_allocated_bytes": torch.cuda.memory_allocated(),
                "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated()}
    return {}


def bucket_score(reranker: Any, pairs: list[list[str]], *, batch_size: int = 8,
                 record_inputs: bool = True) -> tuple[list[float], list[str], dict[str, int]]:
    import torch
    prepared = prepare_inputs(reranker, pairs)
    lengths = [len(row["input_ids"]) for row in prepared]
    plan = batch_plan(lengths, batch_size, bucket=True)
    outputs = []
    with torch.inference_mode():
        for indices in plan:
            inputs = reranker.tokenizer.pad([prepared[index] for index in indices], padding=True, return_tensors="pt")
            tensors = {key: value.to(reranker.device) for key, value in inputs.items()}
            logits = reranker.model(**tensors, return_dict=True).logits.float()
            if tuple(logits.shape) != (len(indices), 1) or not torch.isfinite(logits).all():
                raise ValueError("Invalid candidate reranker logits")
            outputs.append(logits[:, 0].sigmoid().cpu().tolist())
    hashes = [input_hash(row) for row in prepared] if record_inputs else []
    return scatter_scores(plan, outputs, len(pairs)), hashes, padding_work(lengths, plan)


class ForwardCapture:
    """Observe actual production unpadded IDs/masks separately from timing."""
    def __init__(self, original: Any) -> None:
        self.original = original
        self.hashes: list[str] = []
        self.lengths: list[int] = []

    def __call__(self, **inputs: Any) -> Any:
        sequences = {key: value.detach().cpu().tolist() for key, value in inputs.items() if key != "return_dict"}
        for index, attention in enumerate(sequences["attention_mask"]):
            row = {key: [value for value, keep in zip(values[index], attention, strict=True) if keep]
                   for key, values in sequences.items()}
            self.hashes.append(input_hash(row))
            self.lengths.append(len(row["input_ids"]))
        return self.original(**inputs)


def worker(directory: Path, model: str, batch_size: int, bucket: bool) -> None:
    server = directory / "server"
    if Path.cwd().resolve() != server.resolve() or not directory.name.startswith("orb_rerank_"):
        raise ValueError("Reranker experiment requires its owned source-copy process")
    sys.path.insert(0, str(server))
    import torch
    torch.set_num_threads(4)
    from services.local_transformers import LocalReranker
    documents, cases, floor = public_workload(server)
    started = time.perf_counter()
    scorer = LocalReranker(model, use_fp16=True)
    synchronize(torch, scorer.device)
    report: dict[str, Any] = {"batch_size": batch_size, "length_bucket": bucket, "max_length": 512,
        "query_max_length": 384, "normalize": True, "requested_fp16": True,
        "device": str(scorer.device), "actual_dtype": str(next(scorer.model.parameters()).dtype),
        "load_s": time.perf_counter() - started, "model": snapshot_proof(model, reranker=True),
        "source_sha256": {name: hashlib.sha256((server / name).read_bytes()).hexdigest() for name in SOURCES},
        "document_ids": [row["id"] for row in documents], "relevance_floor": floor,
        "quality_rows": [], "timing_calls": [], "device_memory_samples": []}
    warm_pairs = [[cases[0]["query"], row["passage"]] for row in documents[:12]]
    if bucket:
        bucket_score(scorer, warm_pairs, batch_size=batch_size)
    else:
        scorer.compute_score(warm_pairs, batch_size=batch_size, max_length=512, normalize=True)
    synchronize(torch, scorer.device)
    for case in cases:
        pairs = [[case["query"], row["passage"]] for row in documents]
        if bucket:
            scores, hashes, work = bucket_score(scorer, pairs, batch_size=batch_size)
        else:
            capture = ForwardCapture(scorer.model)
            original = scorer.model
            scorer.model = capture
            try:
                scores = scorer.compute_score(pairs, batch_size=batch_size, max_length=512, normalize=True)
            finally:
                scorer.model = original
            hashes = capture.hashes
            work = padding_work(capture.lengths, batch_plan(capture.lengths, batch_size))
        report["quality_rows"].append({"id": case["id"], "query_sha256": case["query_sha256"],
            "expected_corpus_ids": [name for name in case["expected"] if name in report["document_ids"]],
            "scores": scores, "prepared_input_hashes": hashes, "padding": work})
        report["device_memory_samples"].append(device_memory(torch, scorer.device))
    # The timing path has no forward capture / tensor copies. Twelve candidates
    # matches the current rerank pool. Diverse static queries include negatives.
    indices = sorted({0, 2, 7, 13, len(cases) // 2, len(cases) - 1})
    for repeat in range(2):
        for index in indices:
            case = cases[index]
            selected = [documents[(index * 7 + offset) % len(documents)] for offset in range(12)]
            pairs = [[case["query"], row["passage"]] for row in selected]
            synchronize(torch, scorer.device)
            begin = time.perf_counter()
            if bucket:
                values, _hashes, _work = bucket_score(scorer, pairs, batch_size=batch_size, record_inputs=False)
            else:
                values = scorer.compute_score(pairs, batch_size=batch_size, max_length=512, normalize=True)
            synchronize(torch, scorer.device)
            if len(values) != 12:
                raise ValueError("Dropped timing scores")
            report["timing_calls"].append({"case": case["id"], "repeat": repeat, "pairs": 12,
                                             "elapsed_s": time.perf_counter() - begin, "scores": values})
            report["device_memory_samples"].append(device_memory(torch, scorer.device))
    import resource
    report["maximum_process_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["rss_units"] = "bytes_on_Darwin"
    (directory / "worker-result.json").write_text(json.dumps(report, allow_nan=False) + "\n")


async def terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            await asyncio.to_thread(process.wait, 5)
        except ProcessLookupError:
            pass


async def experiment(model: str, output: Path) -> int:
    snapshot_proof(model, reranker=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    initial = await asyncio.to_thread(inspect_memory)
    if initial.free_percentage < 20 or initial.estimated_available_bytes < 12 * GIB:
        raise ValueError("Insufficient estimated memory headroom for the candidate experiment")
    report: dict[str, Any] = {"scope": "script_only_static_public_corpus_no_database_no_production_change",
        "initial_memory": asdict(initial), "pressure_samples": [], "configurations": [],
        "quality_scope": "Numerical/ranking/threshold equivalence to current batch8; not independent clinical validation"}
    overall = time.monotonic()
    low = 0
    for batch_size, bucket in ((8, False), (16, False), (32, False), (8, True)):
        with tempfile.TemporaryDirectory(prefix="orb_rerank_") as temporary:
            directory = Path(temporary)
            server = directory / "server"
            for source in SOURCES:
                destination = server / source
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / "server" / source, destination)
            (server / "services/__init__.py").touch()
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--directory", str(directory),
                       "--model", model, "--batch-size", str(batch_size)]
            if bucket:
                command.append("--bucket")
            env = isolated_environment(directory)
            env.update({"OMP_NUM_THREADS": "4", "TOKENIZERS_PARALLELISM": "false"})
            with (directory / "worker.log").open("w") as log:
                process = subprocess.Popen(command, cwd=server, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while process.poll() is None:
                        await asyncio.sleep(2)
                        current = await asyncio.to_thread(inspect_memory)
                        low = low + 1 if current.free_percentage < 15 else 0
                        report["pressure_samples"].append({"batch_size": batch_size, "bucket": bucket,
                            "elapsed_s": time.monotonic() - overall, **asdict(current), **vm_counters(current)})
                        reason = pressure_abort_reason(initial, current, low)
                        if reason or time.monotonic() - overall > 300:
                            report["aborted"] = reason or "bounded300second_experiment_limit"
                            report["elapsed_s"] = time.monotonic() - overall
                            output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
                            raise RuntimeError(reason or "bounded300second_experiment_limit")
                    if process.returncode != 0 or not (directory / "worker-result.json").is_file():
                        report["aborted"] = "owned_reranker_candidate_failed"
                        output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
                        raise RuntimeError("Owned reranker candidate failed; raw log deliberately not persisted")
                    report["configurations"].append(json.loads((directory / "worker-result.json").read_text()))
                finally:
                    await terminate(process)
        # Save each completed phase so interruption never invents a full pass.
        output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    reference = report["configurations"][0]
    for candidate in report["configurations"]:
        rows = candidate["quality_rows"]
        same_inputs = len(rows) == len(reference["quality_rows"]) and all(
            new["prepared_input_hashes"] == old["prepared_input_hashes"]
            for old, new in zip(reference["quality_rows"], rows, strict=True))
        comparison = compare_scores([row["scores"] for row in reference["quality_rows"]],
                                    [row["scores"] for row in rows], reference["relevance_floor"])
        comparison["exact_unpadded_inputs_equal"] = same_inputs
        same_sources = candidate["source_sha256"] == reference["source_sha256"]
        same_model = (candidate["model"], candidate["device"], candidate["actual_dtype"]) == (
            reference["model"], reference["device"], reference["actual_dtype"])
        comparison.update(identical_source_fingerprints=same_sources, identical_model_device_dtype=same_model)
        comparison["quality_equivalence_passed"] &= same_inputs and same_sources and same_model
        twelve = compare_scores([row["scores"] for row in reference["timing_calls"]],
                                [row["scores"] for row in candidate["timing_calls"]], reference["relevance_floor"])
        comparison["twelve_candidate_quality"] = twelve
        same_cases = [(row["case"], row["repeat"]) for row in candidate["timing_calls"]] == [
            (row["case"], row["repeat"]) for row in reference["timing_calls"]]
        comparison["quality_equivalence_passed"] &= twelve["quality_equivalence_passed"] and same_cases
        elapsed = [row["elapsed_s"] for row in candidate["timing_calls"]]
        baseline_elapsed = [row["elapsed_s"] for row in reference["timing_calls"]]
        comparison.update(timing_median_s=statistics.median(elapsed), timing_max_s=max(elapsed),
            timing_pairs_per_second=12 * len(elapsed) / sum(elapsed),
            median_speedup=statistics.median(baseline_elapsed) / statistics.median(elapsed))
        candidate["comparison_with_batch8"] = comparison
    report["elapsed_s"] = time.monotonic() - overall
    report["final_memory"] = asdict(await asyncio.to_thread(inspect_memory))
    report["all_configs_completed"] = True
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"report": str(output), "configurations": [
        {"batch_size": row["batch_size"], "bucket": row["length_bucket"], **row["comparison_with_batch8"]}
        for row in report["configurations"]]}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=local_snapshot_path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--batch-size", type=int, choices=(8, 16, 32), default=8)
    parser.add_argument("--bucket", action="store_true")
    args = parser.parse_args()
    if args.worker:
        if args.directory is None:
            parser.error("worker requires its owned directory")
        worker(args.directory, args.model, args.batch_size, args.bucket)
        return 0
    if not args.execute or args.output is None:
        parser.error("experiment requires --execute and a new report path")
    if args.output.exists():
        parser.error("refusing to overwrite an earlier experiment")
    try:
        return asyncio.run(experiment(args.model, args.output))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Reranker experiment refused: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
