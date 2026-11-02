"""Small CPU-only composition fixtures; no real models, GPU or hash scans."""
from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
from pathlib import Path

import numpy as np
import pytest
from safetensors.numpy import load_file, save_file


@pytest.fixture
def composer(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("compose_medgemma_mlx_candidate")
    monkeypatch.setattr(module, "SOURCE_TEXT_TENSORS", 3)
    monkeypatch.setattr(module, "SOURCE_TEXT_PARAMETERS", 1026)
    monkeypatch.setattr(module, "MATRIX_COUNTS", {12: 1, 14: 1})
    monkeypatch.setattr(module, "NORM_COUNT", 1)
    monkeypatch.setattr(module, "VISION_COUNT", 0)
    monkeypatch.setattr(module, "ARCHITECTURE_PINS", {"model_type": "gemma3_text", "hidden_size": 256})
    monkeypatch.setattr(module, "tensor_mapping", lambda name: ({"q4-source": "q4.weight", "q6-source": "q6.weight"}[name], False))
    monkeypatch.setattr(module, "expected_shapes", lambda config: {"q4.weight": (2, 256), "q6.weight": (2, 256), "norm.weight": (2,)})
    return module


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def parent_fixture(composer, root, *, repack):
    root.mkdir()
    q4_bits, q6_bits = (4, None) if repack else (8, 8)
    matrices = [{"source": "q4-source", "target": "q4.weight", "shape": [2, 256], "source_type": 12,
                 "native_bits": q4_bits, "source_integer_codes_preserved": repack},
                {"source": "q6-source", "target": "q6.weight", "shape": [2, 256], "source_type": 14,
                 "native_bits": q6_bits}]
    weight_map = {}
    payload = 0
    for i, (name, bits) in enumerate((("q4.weight", q4_bits), ("q6.weight", q6_bits), ("norm.weight", None)), 1):
        filename = f"model-{i:05d}-of-00003.safetensors"
        if name == "norm.weight":
            arrays = {name: np.array([0.0, -0.5], np.float32)}
        elif bits is None:
            arrays = {name: np.full((2, 256), 0.123, np.float16)}
        else:
            prefix = name.removesuffix(".weight")
            arrays = {name: np.full((2, 256 * bits // 32), 0x76543210, np.uint32),
                      prefix + ".scales": np.full((2, 8), 0.001, np.float16),
                      prefix + ".biases": np.full((2, 8), -0.01, np.float16)}
        save_file(arrays, root / filename)
        payload += sum(value.nbytes for value in arrays.values())
        weight_map.update({key: filename for key in arrays})
    config = {"model_type": "gemma3_text", "hidden_size": 256,
              "orb_candidate_label": composer.REPACK4_F16_CANDIDATE_LABEL if repack else composer.AFFINE8_CANDIDATE_LABEL,
              "orb_quantisation_profile": "source-repack4-f16" if repack else "affine8",
              "orb_clinical_quality_qualified": False, "quantization": {"bits": q4_bits, "group_size": 32, "mode": "affine"}}
    write_json(root / "config.json", config)
    write_json(root / "model.safetensors.index.json", {"metadata": {"total_size": payload}, "weight_map": weight_map})
    for name in composer.COMMON_METADATA:
        (root / name).write_text("template" if name.endswith("jinja") else "{}")
    files = {}
    for file in root.iterdir():
        entry = {"bytes": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
        if file.suffix == ".safetensors":
            entry["tensor_data_bytes"] = sum(array.nbytes for array in load_file(file).values())
        files[file.name] = entry
    manifest = {"status": "converted_candidate_unqualified", "candidate_label": config["orb_candidate_label"],
                "quantisation_profile": config["orb_quantisation_profile"], "source_sha256": composer.SOURCE_SHA256,
                "source_bytes": composer.SOURCE_BYTES, "source_text_tensors": 3, "source_text_parameters": 1026,
                "clinical_quality_qualified": False, "lossless_conversion": False,
                "fresh_quantization_performed": not repack,
                "excluded_vision_projector_tensors": [], "files": files, "matrices": matrices}
    write_json(root / "conversion-manifest.json", manifest)
    return root


@pytest.fixture
def parents(composer, tmp_path):
    return parent_fixture(composer, tmp_path / "repack", repack=True), parent_fixture(composer, tmp_path / "affine8", repack=False)


def snapshot(directory):
    return {file.name: (file.read_bytes(), file.stat().st_mode) for file in directory.iterdir()}


def test_dry_plan_selects_q4_repack_q6_affine8_and_norms_without_writing_or_weight_hashes(composer, parents, tmp_path, monkeypatch):
    before = [snapshot(parent) for parent in parents]
    monkeypatch.setattr(composer, "_verify_stream", lambda *args: (_ for _ in ()).throw(AssertionError("No payload hashing in dry plan")))
    plan = composer.plan_composition(*parents)
    assert plan["payload_bytes"] == 904
    assert [(r["target"], r["origin"], r["bits"]) for r in plan["selected"]] == [
        ("norm.weight", "q4_repack", None), ("q4.weight", "q4_repack", 4), ("q6.weight", "affine8", 8)]
    output = tmp_path / "not-created"
    assert composer.main(["--q4-repack-parent", str(parents[0]), "--affine8-parent", str(parents[1]), "--output-dir", str(output)]) == 0
    assert not output.exists()
    assert [snapshot(parent) for parent in parents] == before


@pytest.mark.parametrize("copy_fallback", [False, True])
def test_execution_preserves_parents_and_exact_selected_payloads_with_hardlink_or_copy(composer, parents, tmp_path, monkeypatch, copy_fallback):
    before = [snapshot(parent) for parent in parents]
    if copy_fallback:
        def unavailable(*args):
            raise OSError(errno.EXDEV, "synthetic cross-device link")
        monkeypatch.setattr(composer.os, "link", unavailable)
    plan = composer.plan_composition(*parents)
    output = tmp_path / "composed"
    manifest = composer.compose(plan, output)
    assert manifest["status"] == "converted_candidate_unqualified"
    assert manifest["clinical_quality_qualified"] is False and manifest["lossless_conversion"] is False
    assert manifest["selected_weight_hashes_verified"] is True
    assert manifest["unselected_parent_weight_hashes_verified"] is False
    assert manifest["candidate_label"] == composer.CANDIDATE_LABEL
    config = json.loads((output / "config.json").read_text())
    assert config["quantization"]["q4"]["bits"] == 4
    assert config["quantization"]["q6"]["bits"] == 8
    assert "norm" not in config["quantization"]
    index = json.loads((output / "model.safetensors.index.json").read_text())
    assert len(index["weight_map"]) == 7
    assert index["metadata"]["total_size"] == 904
    for row in plan["selected"]:
        dest = output / row["output_name"]
        assert dest.read_bytes() == row["source_file"].read_bytes()
        assert (dest.stat().st_ino == row["source_file"].stat().st_ino) is not copy_fallback
    for name, record in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == record["sha256"]
    assert [snapshot(parent) for parent in parents] == before
    assert not (output / ".conversion-incomplete").exists()


@pytest.mark.parametrize("change", ["source", "incomplete", "status", "matrix_count", "matrix_bits", "tokenizer"])
def test_mismatched_or_incomplete_parents_are_refused_before_any_composition(composer, parents, change):
    p = parents[1] / "conversion-manifest.json"
    manifest = json.loads(p.read_text())
    if change == "source":
        manifest["source_sha256"] = "0" * 64
    elif change == "incomplete":
        (parents[1] / ".conversion-incomplete").touch()
    elif change == "status":
        manifest["status"] = "converting"
    elif change == "matrix_count":
        manifest["matrices"].pop()
    elif change == "matrix_bits":
        manifest["matrices"][1]["native_bits"] = 6
    else:
        file = parents[1] / "tokenizer.json"
        file.write_text('{"different":true}')
        manifest["files"][file.name] = {"bytes": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    write_json(p, manifest)
    with pytest.raises(composer.CompositionRefused):
        composer.plan_composition(*parents)


def test_selected_weight_changed_after_plan_keeps_incomplete_marker_and_fails_hash(composer, parents, tmp_path):
    plan = composer.plan_composition(*parents)
    row = next(r for r in plan["selected"] if r["origin"] == "affine8")
    file = row["source_file"]
    with file.open("r+b") as stream:
        stream.seek(-1, os.SEEK_END)
        value = stream.read(1)
        stream.seek(-1, os.SEEK_END)
        stream.write(bytes([value[0] ^ 1]))
    output = tmp_path / "incomplete"
    with pytest.raises(composer.CompositionRefused, match="digest"):
        composer.compose(plan, output)
    assert (output / ".conversion-incomplete").exists()
    assert json.loads((output / "conversion-manifest.json").read_text())["status"] == "failed_incomplete_do_not_load"
    assert not (output / "config.json").exists()


def test_execute_requires_acknowledgement_before_parent_reads(composer, parents, tmp_path, monkeypatch):
    monkeypatch.setattr(composer, "plan_composition", lambda *a: (_ for _ in ()).throw(AssertionError("Must not inspect")))
    assert composer.main(["--q4-repack-parent", str(parents[0]), "--affine8-parent", str(parents[1]),
                          "--output-dir", str(tmp_path / "not-created"), "--execute"]) == 2
