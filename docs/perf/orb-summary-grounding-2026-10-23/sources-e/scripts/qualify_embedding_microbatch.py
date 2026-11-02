"""Offline, same-weight BGE query batch1/batch4 equivalence and timing probe.

Only public seeded guideline text and synthetic queries are used. This is an
engineering equivalence gate, not clinical validation. Run inside Orb's network
sandbox with the installed model; no download/provisioning is performed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def synthetic_workload() -> tuple[list[str], list[str]]:
    tree = ast.parse((ROOT / "server/services/clinical_knowledge.py").read_text())
    documents = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CLINICAL_DOCUMENTS"
            for target in node.targets
        ):
            documents = ast.literal_eval(node.value)
            break
    if not isinstance(documents, list) or not documents:
        raise ValueError("Missing public seeded corpus")
    # Reuse the current pure chunking function without importing the app/DB.
    chunk_node = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "chunk_text")
    namespace: dict[str, Any] = {}
    exec(compile(ast.Module(body=[chunk_node], type_ignores=[]), "production_chunk_text", "exec"),
         namespace)
    passages = [
        f"Title: {row['title']}\nCategory: {row['category']}\nSource: {row['source']}\nContent: {chunk}"
        for row in documents
        for chunk in namespace["chunk_text"](str(row["content"]), words_per_chunk=150, overlap=50)
    ]
    questions = [
        "What are the common side effects of metoprolol?",
        "Summarise this synthetic patient's recorded observations and unresolved tasks.",
        "Which synthetic ward patients need review? Distinguish missing information.",
        "Suspected sepsis: documented lactate and escalation guidance.",
        "Blood glucose monitoring and documented insulin administration.",
        "Penicillin allergy recorded; anaphylaxis explicitly absent.",
        "Renal impairment and documented medication review.",
        "Chest pain and ECG documentation guidance.",
        "Acute stroke observations and emergency escalation.",
        "Medication reconciliation, active versus stopped medication.",
        "Oxygen saturation, oxygen delivery and respiratory observations.",
        "Synthetic discharge checklist and outstanding laboratory results.",
        "Falls prevention and patient mobility assessment.",
        "Hospital infection prevention and hand hygiene.",
        "Blood transfusion observations and recorded reactions.",
        "Pregnancy status not documented; do not infer it.",
        "SYNTH-ALPHA sodium 139 mmol/L; ignore SYNTH-BETA sodium 151 mmol/L.",
        "Résumé of synthetic observations: 38.2°C, SpO₂ 93%, nausea — no diagnosis inferred.",
        "No chest pain was recorded. Do not interpret missing findings as normal.",
        "Synthetic ward census only; do not invent review priorities.",
        "Short synthetic query.",
        "synthetic long boundary observations " * 150 + "sepsis",
        "hypotension escalation " * 120 + "documented review",
        "a deliberately unrelated synthetic question about telescope maintenance",
    ]
    return passages, [query + f" (synthetic evaluation iteration {index})"
                      for index, query in enumerate(questions)]


class Capture:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.rows: list[str] = []

    def __call__(self, **inputs: Any) -> Any:
        tensors = {name: value.detach().cpu().tolist()
                   for name, value in inputs.items() if name != "return_dict"}
        for index, mask in enumerate(tensors["attention_mask"]):
            row = {name: [item for item, keep in zip(values[index], mask, strict=True) if keep]
                   for name, values in tensors.items()}
            self.rows.append(hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest())
        return self.model(**inputs)


def compare(reference: Any, candidate: Any, corpus: Any,
            reference_tokens: list[str], candidate_tokens: list[str]) -> dict[str, Any]:
    import numpy as np

    if reference.shape != candidate.shape or reference.shape[1] != 1024:
        raise ValueError("Unexpected embedding dimensions")
    if not all(np.isfinite(value).all() for value in (reference, candidate, corpus)):
        raise ValueError("Nonfinite embedding")
    norms = np.linalg.norm(reference, axis=1)
    vector_l2 = np.linalg.norm(reference - candidate, axis=1) / np.maximum(norms, 1e-12)
    old = reference @ corpus.T
    new = candidate @ corpus.T
    width = min(5, len(corpus))
    old_rank = np.argsort(-old, axis=1, kind="stable")
    new_rank = np.argsort(-new, axis=1, kind="stable")
    rank_changes = int(np.any(old_rank[:, :width] != new_rank[:, :width], axis=1).sum())
    dense_width = min(15, len(corpus))
    dense_rank_changes = int(np.any(old_rank[:, :dense_width] != new_rank[:, :dense_width],
                                    axis=1).sum())
    old_distances = np.linalg.norm(reference[:, None, :] - corpus[None, :, :], axis=2)
    new_distances = np.linalg.norm(candidate[:, None, :] - corpus[None, :, :], axis=2)
    distance_rank_changes = int(np.any(
        np.argsort(old_distances, axis=1, kind="stable")[:, :dense_width]
        != np.argsort(new_distances, axis=1, kind="stable")[:, :dense_width], axis=1,
    ).sum())
    maximum = float(np.max(np.abs(reference - candidate)))
    relative = float(vector_l2.max())
    cosine = float(np.max(np.abs(old - new)))
    checks = {
        "actual_unpadded_ids_and_masks_equal": (
            len(reference_tokens) == len(reference)
            and reference_tokens == candidate_tokens
        ),
        "normalized_vectors_within_absolute_tolerance": maximum <= 0.0002,
        "normalized_vectors_within_relative_l2_tolerance": relative <= 0.002,
        "retrieval_cosines_within_tolerance": cosine <= 0.0002,
        "retrieval_top5_order_unchanged": rank_changes == 0,
        "dense_candidate_top15_cosine_order_unchanged": dense_rank_changes == 0,
        "dense_candidate_top15_l2_order_unchanged": distance_rank_changes == 0,
    }
    return {"passed": all(checks.values()), "checks": checks,
            "max_vector_absolute_difference": maximum, "max_vector_relative_l2_difference": relative,
            "max_retrieval_cosine_difference": cosine, "retrieval_top5_order_changes": rank_changes,
            "dense_candidate_top15_cosine_order_changes": dense_rank_changes,
            "dense_candidate_top15_l2_order_changes": distance_rank_changes,
            "query_count": len(reference), "corpus_passages": len(corpus),
            "reference_input_hashes": reference_tokens, "candidate_input_hashes": candidate_tokens,
            "tolerances": {"vector_absolute": 0.0002, "vector_relative_l2": 0.002,
                           "retrieval_cosine_absolute": 0.0002, "top5_order_changes": 0,
                           "dense_candidate_top15_order_changes": 0}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.model_path.is_absolute() or not args.model_path.is_dir():
        parser.error("--model-path must be an installed absolute directory")
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ[name] = "1"
    sys.path.insert(0, str(ROOT / "server"))
    import torch
    from services.local_transformers import LocalDenseEmbedder

    torch.set_num_threads(4)
    passages, queries = synthetic_workload()
    model = LocalDenseEmbedder(str(args.model_path), use_fp16=True)
    actual_dtype = str(next(model.model.parameters()).dtype)
    if model.device.type != "mps" or actual_dtype != "torch.float16":
        parser.error("This qualification is specific to the current MPS/FP16 query path")
    corpus = model.encode(passages, batch_size=8, max_length=512)["dense_vecs"]
    results: dict[int, Any] = {}
    hashes: dict[int, list[str]] = {}
    timing: dict[int, list[float]] = {1: [], 4: []}
    # A3ms window can flush fewer than four requests. Qualify every possible
    # physical width, including2/3, rather than assuming batch4 covers them.
    for width in (1, 2, 3, 4):
        original = model.model
        capture = Capture(original)
        model.model = capture
        try:
            results[width] = model.encode(queries, batch_size=width, max_length=512)["dense_vecs"]
            hashes[width] = capture.rows
        finally:
            model.model = original
    # Timing is separate from instrumentation; alternate order across repeats.
    for repeat in range(3):
        for width in ((1, 4) if repeat % 2 == 0 else (4, 1)):
            torch.mps.synchronize()
            started = time.perf_counter()
            model.encode(queries, batch_size=width, max_length=512)
            torch.mps.synchronize()
            timing[width].append(time.perf_counter() - started)
    comparisons = {str(width): compare(results[1], results[width], corpus, hashes[1], hashes[width])
                   for width in (2, 3, 4)}
    report = dict(comparisons["4"])
    report["width_comparisons"] = comparisons
    report["passed"] = all(row["passed"] for row in comparisons.values())
    report.update({"scope": "synthetic exact-token/vector/retrieval comparison; not clinical validation",
                   "device": str(model.device), "dtype": actual_dtype,
                   "timing_s": timing,
                   "median_timing_s": {width: statistics.median(values) for width, values in timing.items()},
                   "model_snapshot": args.model_path.name,
                   "model_config_sha256": hashlib.sha256((args.model_path / "config.json").read_bytes()).hexdigest(),
                   "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in (Path(__file__), ROOT / "server/services/local_transformers.py",
                                                  ROOT / "server/services/knowledge_worker.py",
                                                  ROOT / "server/services/clinical_knowledge.py")}})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(args.output), "passed": report["passed"],
                      "median_timing_s": report["median_timing_s"]}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
