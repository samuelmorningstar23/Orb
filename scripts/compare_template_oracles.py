#!/usr/bin/env python3
"""Compare observed Ollama prompt/token IDs with a local candidate tokenizer.

This utility reads tokenizer/configuration metadata and synthetic evidence only.
It never imports MLX, reads model weight payloads, generates answers, downloads
files or changes the converted model. Its only output is a new comparison JSON.
Only the documented Go-completion BOS prefix is reconciled. Whitespace, Unicode,
system separators and every other prompt byte must otherwise match exactly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ollama_template_oracle import OracleRefused, canonical, captured_chats, digest

MAX_ORACLE_BYTES = 8 * 1024 * 1024
MAX_METADATA_FILE_BYTES = 32 * 1024 * 1024
SOURCE_SHA256 = "4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758"


class ComparisonRefused(ValueError):
    """Evidence or local tokenizer metadata cannot establish an exact comparison."""


def _sha256(path: Path) -> str:
    digest_value = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest_value.update(data)
    return digest_value.hexdigest()


def read_object(path: Path, limit: int) -> dict[str, Any]:
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > limit:
        raise ComparisonRefused("Use an existing bounded absolute local JSON evidence file.")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ComparisonRefused("An evidence file must contain a JSON object.")
    return value


def verify_tokenizer_metadata(directory: Path) -> dict[str, Any]:
    """Verify only small tokenizer/config files, never *.safetensors weights."""
    if not directory.is_absolute() or not directory.is_dir() or (directory / ".conversion-incomplete").exists():
        raise ComparisonRefused("A completed local candidate directory is required.")
    manifest_path = directory / "conversion-manifest.json"
    manifest = read_object(manifest_path, MAX_ORACLE_BYTES)
    if (manifest.get("status") != "converted_candidate_unqualified"
            or manifest.get("source_sha256") != SOURCE_SHA256
            or manifest.get("lossless_conversion") is not False
            or not isinstance(manifest.get("files"), dict)):
        raise ComparisonRefused("The candidate conversion provenance is incomplete or changed.")
    names = {"config.json", "tokenizer.json", "tokenizer_config.json"}
    names.update(name for name in ("special_tokens_map.json", "chat_template.jinja") if name in manifest["files"])
    evidence = {}
    for name in sorted(names):
        file = directory / name
        expected = manifest["files"].get(name)
        if (not isinstance(expected, dict) or not file.is_file() or not file.resolve().is_relative_to(directory.resolve())
                or file.stat().st_size > MAX_METADATA_FILE_BYTES or file.stat().st_size != expected.get("bytes")):
            raise ComparisonRefused("A candidate tokenizer/config metadata file is missing or changed.")
        actual = _sha256(file)
        if actual != expected.get("sha256"):
            raise ComparisonRefused("A candidate tokenizer/config metadata hash differs from its manifest.")
        evidence[name] = {"sha256": actual, "bytes": file.stat().st_size}
    return {"directory": str(directory), "conversion_manifest_sha256": _sha256(manifest_path),
            "candidate_label": manifest.get("candidate_label"), "source_sha256": SOURCE_SHA256,
            "verified_metadata_files": evidence, "model_weight_payloads_read": False}


def expected_effective_prompt(record: dict[str, Any], selection: dict[str, Any], bos_token: str) -> tuple[str, str]:
    prompt = record.get("rendered_template")
    if not isinstance(prompt, str) or hashlib.sha256(prompt.encode()).hexdigest() != record.get("rendered_template_sha256"):
        raise ComparisonRefused("An observed prompt is absent or its hash changed.")
    source = record.get("selected_template_source")
    if source != selection.get("source"):
        raise ComparisonRefused("The recorded template source differs from its selection evidence.")
    if source == "selected_gguf_jinja_template":
        return prompt, "No reconciliation: native GGUF chat template includes its own special tokens"
    if source in {"pinned_ollama_go_template", "ollama_renderer"} and selection.get("tokenizer_add_bos_token") is True:
        if bos_token != "<bos>" or selection.get("bos_token_id") != 2:
            raise ComparisonRefused("Only the pinned Gemma3 BOS convention is supported.")
        return bos_token + prompt.removeprefix(bos_token), "Documented Go completion: remove at most one leading <bos>, then add one tokenizer BOS; no other text changes"
    raise ComparisonRefused("The actual template/BOS route is not classified by the oracle.")


def first_difference(left: list[int] | str, right: list[int] | str) -> int | None:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right)) if len(left) != len(right) else None


def compare_cases(oracle: dict[str, Any], captures: list[dict[str, Any]], tokenizer: Any) -> list[dict[str, Any]]:
    selection = oracle.get("template_selection")
    records = oracle.get("oracles")
    if not isinstance(selection, dict) or not isinstance(records, list) or not records:
        raise ComparisonRefused("Observed prompt and template selection records are required.")
    if getattr(tokenizer, "bos_token_id", None) != 2 or getattr(tokenizer, "bos_token", None) != "<bos>":
        raise ComparisonRefused("The local tokenizer must retain Gemma3 BOS ID2.")
    by_id = {row["request_id"]: row for row in captures}
    seen = set()
    results = []
    for record in records:
        if not isinstance(record, dict):
            raise ComparisonRefused("Invalid prompt oracle record.")
        identifier = record.get("request_id")
        if identifier in seen or identifier not in by_id:
            raise ComparisonRefused("An oracle request is duplicated or absent from the capture.")
        seen.add(identifier)
        captured = by_id[identifier]
        if record.get("source_body_sha256") != captured["body_sha256"]:
            raise ComparisonRefused("The oracle and capture request-body identities differ.")
        actual_ids = record.get("native_token_ids")
        if (not isinstance(actual_ids, list) or not actual_ids or any(type(value) is not int or value < 0 for value in actual_ids)
                or digest(actual_ids) != record.get("native_token_ids_sha256")
                or len(actual_ids) != record.get("native_prompt_tokens")):
            raise ComparisonRefused("The oracle must contain exact, unchanged effective native token IDs.")
        expected_prompt, bos_rule = expected_effective_prompt(record, selection, tokenizer.bos_token)
        messages = captured["body"]["messages"]
        candidate_prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        candidate_ids = tokenizer.apply_chat_template(messages, tokenize=True, return_dict=False, add_generation_prompt=True)
        if (not isinstance(candidate_prompt, str) or not isinstance(candidate_ids, list) or not candidate_ids
                or any(type(value) is not int or value < 0 for value in candidate_ids)):
            raise ComparisonRefused("The local tokenizer did not provide a string prompt and flat integer IDs.")
        # The same exact candidate prompt must account for its own IDs. Merely
        # comparing counts, or iterating a Transformers5 BatchEncoding, is unsafe.
        if tokenizer.encode(candidate_prompt, add_special_tokens=False) != candidate_ids:
            raise ComparisonRefused("Candidate chat-template tokens differ from encoding its exact prompt.")
        token_difference = first_difference(actual_ids, candidate_ids)
        prompt_difference = first_difference(expected_prompt, candidate_prompt)
        results.append({"request_id": identifier, "source_body_sha256": captured["body_sha256"],
                        "exact_effective_token_ids_match": token_difference is None,
                        "exact_prompt_after_documented_bos_match": prompt_difference is None,
                        "bos_reconciliation": bos_rule, "whitespace_normalization_performed": False,
                        "native_prompt_tokens": len(actual_ids), "candidate_prompt_tokens": len(candidate_ids),
                        "native_token_ids_sha256": digest(actual_ids), "candidate_token_ids_sha256": digest(candidate_ids),
                        "oracle_effective_prompt_sha256": hashlib.sha256(expected_prompt.encode()).hexdigest(),
                        "candidate_rendered_prompt_sha256": hashlib.sha256(candidate_prompt.encode()).hexdigest(),
                        "first_different_token_index": token_difference,
                        "native_token_at_difference": actual_ids[token_difference] if token_difference is not None and token_difference < len(actual_ids) else None,
                        "candidate_token_at_difference": candidate_ids[token_difference] if token_difference is not None and token_difference < len(candidate_ids) else None,
                        "first_different_prompt_character_index": prompt_difference})
    return results


def compare_oracles(oracle_path: Path, model_dir: Path, output: Path, *, capture_jsonl: Path | None = None,
                    tokenizer: Any = None) -> dict[str, Any]:
    """Create comparison evidence for the observed subset; never alter metadata."""
    oracle = read_object(oracle_path, MAX_ORACLE_BYTES)
    software = oracle.get("software")
    if (oracle.get("status") != "oracles_collected" or oracle.get("benchmark_only") is not True
            or oracle.get("synthetic_data_only") is not True or oracle.get("generation_performed") is not False
            or not isinstance(software, dict) or software.get("version") != "0.40.4"):
        raise ComparisonRefused("A completed synthetic render-only Ollama0.40.4 oracle is required.")
    capture_info = oracle.get("capture")
    if not isinstance(capture_info, dict):
        raise ComparisonRefused("Original capture identity is required.")
    path = capture_jsonl or Path(str(capture_info.get("path", "")))
    captures, actual_capture = captured_chats(path)
    if actual_capture["sha256"] != capture_info.get("sha256") or actual_capture["chat_requests"] != capture_info.get("chat_requests"):
        raise ComparisonRefused("The capture file differs from the oracle's exact source.")
    tokenizer_evidence = verify_tokenizer_metadata(model_dir)
    if not output.is_absolute() or output.exists() or not output.parent.is_dir():
        raise ComparisonRefused("Use a new absolute comparison JSON destination.")
    if tokenizer is None:
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
    cases = compare_cases(oracle, captures, tokenizer)
    selection = oracle.get("selection")
    if (not isinstance(selection, dict) or selection.get("selected_count") != len(cases)
            or selection.get("total_captured_chats") != len(captures)
            or selection.get("unselected_count") != len(captures) - len(cases)):
        raise ComparisonRefused("The declared oracle subset differs from its observed cases.")
    all_match = all(row["exact_effective_token_ids_match"] and row["exact_prompt_after_documented_bos_match"] for row in cases)
    report = {"status": "observed_subset_exact_match" if all_match else "observed_subset_mismatch",
              "benchmark_only": True, "synthetic_data_only": True, "model_weight_payloads_read": False,
              "inference_performed": False, "metadata_modified": False, "whitespace_normalization_performed": False,
              "oracle_path": str(oracle_path), "oracle_sha256": _sha256(oracle_path),
              "capture": actual_capture, "tokenizer": tokenizer_evidence, "selection": selection,
              "observed_cases": len(cases), "matching_cases": sum(row["exact_effective_token_ids_match"] and row["exact_prompt_after_documented_bos_match"] for row in cases),
              "template_parity_verified_for_observed_subset": all_match,
              "template_parity_verified_for_entire_capture": all_match and len(cases) == len(captures),
              "clinical_quality_qualified": False, "cases": cases,
              "limitations": ["Exact prompt/token parity is established only for the observed subset, not every possible future prompt.",
                              "Tokenizer parity does not establish quantized-weight, numerical or clinical equivalence."]}
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(report) + b"\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-jsonl", type=Path)
    args = parser.parse_args()
    try:
        report = compare_oracles(args.oracle, args.model_dir, args.output, capture_jsonl=args.capture_jsonl)
        print(json.dumps({"status": report["status"], "observed_cases": report["observed_cases"],
                          "matching_cases": report["matching_cases"], "output": str(args.output)}))
        return 0 if report["template_parity_verified_for_observed_subset"] else 1
    except (ComparisonRefused, OracleRefused, OSError, ValueError) as exc:
        print(json.dumps({"status": "comparison_refused", "error_type": type(exc).__name__, "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
