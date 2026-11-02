"""Compare exact synthetic token oracles without MLX, models or network access."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
for name in ("ollama_template_oracle", "compare_template_oracles"):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
comparison = sys.modules["compare_template_oracles"]
oracle_module = sys.modules["ollama_template_oracle"]


class Tokenizer:
    bos_token = "<bos>"
    bos_token_id = 2

    def __init__(self, prompt, ids):
        self.prompt, self.ids = prompt, ids
        self.calls = []

    def apply_chat_template(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if kwargs["tokenize"]:
            assert kwargs["return_dict"] is False
            return self.ids
        return self.prompt

    def encode(self, text, *, add_special_tokens):
        assert text == self.prompt
        assert add_special_tokens is False
        return self.ids


def evidence(*, raw_prompt="Go prompt  \n", ids=None, source="pinned_ollama_go_template"):
    ids = [2, 105, 1001, 2364] if ids is None else ids
    body = {"model": "medgemma:27b", "messages": [{"role": "system", "content": "Synthetic only"},
                                                    {"role": "user", "content": "Na⁺ 141 mmol/L"}]}
    capture = {"request_id": "a" * 32, "body": body, "body_sha256": comparison.digest(body),
               "backend": "ollama", "endpoint": "/api/chat"}
    record = {"request_id": capture["request_id"], "source_body_sha256": capture["body_sha256"],
              "rendered_template": raw_prompt, "rendered_template_sha256": hashlib.sha256(raw_prompt.encode()).hexdigest(),
              "selected_template_source": source, "native_token_ids": ids,
              "native_token_ids_sha256": comparison.digest(ids), "native_prompt_tokens": len(ids)}
    oracle = {"template_selection": {"source": source, "tokenizer_add_bos_token": True, "bos_token_id": 2},
              "oracles": [record]}
    expected_prompt = raw_prompt if source == "selected_gguf_jinja_template" else "<bos>" + raw_prompt.removeprefix("<bos>")
    return oracle, [capture], Tokenizer(expected_prompt, ids)


@pytest.mark.parametrize("raw", ["Go prompt  \n", "<bos>Go prompt  \n"])
def test_exact_ids_and_only_documented_go_bos_prefix_are_reconciled(raw):
    oracle, captures, tokenizer = evidence(raw_prompt=raw)
    result = comparison.compare_cases(oracle, captures, tokenizer)[0]
    assert result["exact_effective_token_ids_match"] is True
    assert result["exact_prompt_after_documented_bos_match"] is True
    assert result["whitespace_normalization_performed"] is False
    assert tokenizer.calls[0][0] == captures[0]["body"]["messages"]


def test_native_gguf_template_is_compared_verbatim_without_bos_changes():
    oracle, captures, tokenizer = evidence(raw_prompt="<bos>\n<start_of_turn>user\n Q \n", source="selected_gguf_jinja_template")
    result = comparison.compare_cases(oracle, captures, tokenizer)[0]
    assert result["exact_prompt_after_documented_bos_match"] is True
    assert result["bos_reconciliation"].startswith("No reconciliation")


def test_comparison_passes_two_leading_system_messages_unchanged_to_tokenizer():
    oracle, captures, tokenizer = evidence()
    messages = captures[0]["body"]["messages"]
    messages.insert(1, {"role": "system", "content": "[CONTEXT] Synthetic patient chart only. [/CONTEXT]"})
    captures[0]["body_sha256"] = comparison.digest(captures[0]["body"])
    oracle["oracles"][0]["source_body_sha256"] = captures[0]["body_sha256"]
    comparison.compare_cases(oracle, captures, tokenizer)
    assert all(call[0] is messages for call in tokenizer.calls)
    assert [message["role"] for message in tokenizer.calls[0][0]] == ["system", "system", "user"]


@pytest.mark.parametrize("changed", ["<bos>Go prompt\n", "<bos>Go prompt  ", "<bos>Go prompt  \n\n",
                                     "<bos>Go\u00a0prompt  \n", "<bos>Gο prompt  \n"])
def test_whitespace_and_unicode_changes_never_disappear_even_when_ids_match(changed):
    oracle, captures, tokenizer = evidence()
    tokenizer.prompt = changed
    result = comparison.compare_cases(oracle, captures, tokenizer)[0]
    assert result["exact_effective_token_ids_match"] is True
    assert result["exact_prompt_after_documented_bos_match"] is False
    assert result["first_different_prompt_character_index"] is not None
    assert result["whitespace_normalization_performed"] is False


@pytest.mark.parametrize("ids,index", [([2, 105, 999, 2364], 2), ([2, 105], 2), ([2, 105, 1001, 2364, 3], 4)])
def test_same_prompt_but_changed_extra_or_missing_tokens_fail_exact_id_comparison(ids, index):
    oracle, captures, tokenizer = evidence()
    tokenizer.ids = ids
    result = comparison.compare_cases(oracle, captures, tokenizer)[0]
    assert result["exact_prompt_after_documented_bos_match"] is True
    assert result["exact_effective_token_ids_match"] is False
    assert result["first_different_token_index"] == index


@pytest.mark.parametrize("change", ["body", "prompt_hash", "id_hash", "count", "missing_ids", "unknown_route", "duplicate"])
def test_changed_or_incomplete_oracle_identity_is_refused(change):
    oracle, captures, tokenizer = evidence()
    record = oracle["oracles"][0]
    if change == "body":
        record["source_body_sha256"] = "changed"
    elif change == "prompt_hash":
        record["rendered_template"] += "changed"
    elif change == "id_hash":
        record["native_token_ids"][0] = 999
    elif change == "count":
        record["native_prompt_tokens"] = 1
    elif change == "missing_ids":
        record["native_token_ids"] = None
    elif change == "unknown_route":
        record["selected_template_source"] = oracle["template_selection"]["source"] = "unknown"
    else:
        oracle["oracles"].append(dict(record))
    with pytest.raises(comparison.ComparisonRefused):
        comparison.compare_cases(oracle, captures, tokenizer)


def test_tokenizer_must_encode_its_exact_template_instead_of_returning_different_ids():
    oracle, captures, tokenizer = evidence()
    tokenizer.encode = lambda *a, **kw: [2, 7]
    with pytest.raises(comparison.ComparisonRefused):
        comparison.compare_cases(oracle, captures, tokenizer)


@pytest.mark.parametrize("bos,id_value", [("<s>", 2), ("<bos>", 1)])
def test_changed_candidate_bos_token_is_refused(bos, id_value):
    oracle, captures, tokenizer = evidence()
    tokenizer.bos_token, tokenizer.bos_token_id = bos, id_value
    with pytest.raises(comparison.ComparisonRefused):
        comparison.compare_cases(oracle, captures, tokenizer)


def candidate_metadata(tmp_path):
    directory = tmp_path / "model"
    directory.mkdir()
    files = {}
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja"):
        file = directory / name
        file.write_text("{}" if name.endswith("json") else "template")
        files[name] = {"bytes": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    # This deliberately nonexistent weight must never be opened by metadata-only
    # comparison. Weight integrity is established by the separate model loader.
    files["model-00001.safetensors"] = {"bytes": 1_000_000_000, "sha256": "do-not-read"}
    manifest = {"status": "converted_candidate_unqualified", "source_sha256": comparison.SOURCE_SHA256,
                "candidate_label": "requantized-text-only-candidate", "lossless_conversion": False, "files": files}
    (directory / "conversion-manifest.json").write_text(json.dumps(manifest))
    return directory


def test_metadata_verification_reads_no_model_weights_and_changes_nothing(tmp_path):
    directory = candidate_metadata(tmp_path)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    result = comparison.verify_tokenizer_metadata(directory)
    assert result["model_weight_payloads_read"] is False
    assert "model-00001.safetensors" not in result["verified_metadata_files"]
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize("change", ["incomplete", "tokenizer_hash", "source", "status"])
def test_incomplete_conversion_or_modified_tokenizer_metadata_is_refused(tmp_path, change):
    directory = candidate_metadata(tmp_path)
    manifest_file = directory / "conversion-manifest.json"
    manifest = json.loads(manifest_file.read_text())
    if change == "incomplete":
        (directory / ".conversion-incomplete").touch()
    elif change == "tokenizer_hash":
        (directory / "tokenizer.json").write_text("changed")
    elif change == "source":
        manifest["source_sha256"] = "other"
    else:
        manifest["status"] = "converting"
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(comparison.ComparisonRefused):
        comparison.verify_tokenizer_metadata(directory)


def complete_evidence(tmp_path):
    oracle, captures, tokenizer = evidence()
    capture_path = tmp_path / "requests.jsonl"
    capture_path.write_bytes(b"".join(comparison.canonical(row) + b"\n" for row in captures))
    (tmp_path / "proxy-metadata.json").write_text(json.dumps({"benchmark_only": True, "synthetic_data_only": True,
                                                           "backend": "ollama"}))
    _, identity = oracle_module.captured_chats(capture_path)
    oracle.update(status="oracles_collected", benchmark_only=True, synthetic_data_only=True,
                  generation_performed=False, software={"version": "0.40.4"}, capture=identity,
                  selection={"selected_count": 1, "total_captured_chats": 1, "unselected_count": 0})
    oracle_path = tmp_path / "oracle.json"
    oracle_path.write_text(json.dumps(oracle))
    return oracle_path, candidate_metadata(tmp_path), tokenizer


def test_complete_comparison_proves_only_observed_subset_without_loading_weights(tmp_path):
    oracle_path, directory, tokenizer = complete_evidence(tmp_path)
    output = tmp_path / "comparison.json"
    report = comparison.compare_oracles(oracle_path, directory, output, tokenizer=tokenizer)
    assert report["status"] == "observed_subset_exact_match"
    assert report["matching_cases"] == report["observed_cases"] == 1
    assert report["template_parity_verified_for_observed_subset"] is True
    assert report["clinical_quality_qualified"] is False
    assert report["inference_performed"] is False
    assert report["model_weight_payloads_read"] is False
    assert report["metadata_modified"] is False
    assert output.stat().st_mode & 0o777 == 0o600


def test_capture_changes_between_oracle_and_comparison_fail_closed(tmp_path):
    oracle_path, directory, tokenizer = complete_evidence(tmp_path)
    capture = tmp_path / "requests.jsonl"
    row = json.loads(capture.read_text())
    row["body"]["messages"][-1]["content"] = "Changed clinical number 142"
    row["body_sha256"] = comparison.digest(row["body"])
    capture.write_bytes(comparison.canonical(row) + b"\n")
    with pytest.raises(comparison.ComparisonRefused):
        comparison.compare_oracles(oracle_path, directory, tmp_path / "comparison.json", tokenizer=tokenizer)


def test_prompt_mismatch_writes_failure_evidence_without_claiming_parity(tmp_path):
    oracle_path, directory, tokenizer = complete_evidence(tmp_path)
    tokenizer.prompt = tokenizer.prompt.rstrip()
    report = comparison.compare_oracles(oracle_path, directory, tmp_path / "comparison.json", tokenizer=tokenizer)
    assert report["status"] == "observed_subset_mismatch"
    assert report["template_parity_verified_for_observed_subset"] is False
    assert report["matching_cases"] == 0
