#!/usr/bin/env python3
"""Compose existing offline Q4-repack/Q6-affine8 weights; never quantize or infer.

Default mode validates metadata and safetensors headers without hashing weight
payloads or creating output. Execution requires an explicit acknowledgement,
hashes every selected weight byte with bounded F_NOCACHE reads on Darwin, and
hardlinks files into a new directory. Unsupported links fall back to verified
streaming copies. Parent files are never opened for writing or chmod'ed.
"""
from __future__ import annotations

import argparse
import copy
import errno
import hashlib
import json
import math
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

from convert_medgemma_gguf_mlx import (
    AFFINE8_CANDIDATE_LABEL,
    REPACK4_F16_CANDIDATE_LABEL,
    SOURCE_BYTES,
    SOURCE_SHA256,
    SOURCE_TEXT_PARAMETERS,
    SOURCE_TEXT_TENSORS,
    expected_shapes,
    tensor_mapping,
)

CANDIDATE_LABEL = "medgemma-27b-text-mlx-source-q4-integer-repack-q6-affine8-candidate"
PROFILE = "source-repack4-affine8"
MATRIX_COUNTS = {12: 376, 14: 59}
NORM_COUNT = 373
VISION_COUNT = 439
METADATA_LIMIT = 64 * 1024 * 1024
HEADER_LIMIT = 1024 * 1024
IO_CHUNK_BYTES = 1024 * 1024
COMMON_METADATA = ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja", "generation_config.json")
ARCHITECTURE_PINS = {"model_type": "gemma3_text", "hidden_size": 5376, "num_hidden_layers": 62,
                     "intermediate_size": 21504, "num_attention_heads": 32, "num_key_value_heads": 16,
                     "head_dim": 128, "vocab_size": 262208, "rms_norm_eps": 1e-6,
                     "query_pre_attn_scalar": 168, "sliding_window": 1024, "sliding_window_pattern": 6,
                     "max_position_embeddings": 131072, "rope_theta": 1000000.0,
                     "rope_local_base_freq": 10000.0, "rope_scaling": {"type": "linear", "factor": 8.0},
                     "tie_word_embeddings": True, "eos_token_id": [1, 106], "bos_token_id": 2}
HASH = re.compile(r"[0-9a-f]{64}")


class CompositionRefused(ValueError):
    """The selected parents or composed output cannot establish provenance."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file(directory: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
        raise CompositionRefused("Model files must use plain local basenames.")
    file = directory / name
    if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(directory.resolve()):
        raise CompositionRefused("A required model file is absent, symlinked or outside its parent.")
    return file


def _read_metadata(directory: Path, name: str, expected: dict[str, Any] | None = None) -> bytes:
    file = _file(directory, name)
    if file.stat().st_size > METADATA_LIMIT:
        raise CompositionRefused("Metadata exceeds the bounded read limit.")
    data = file.read_bytes()
    if expected is not None and (not isinstance(expected, dict) or len(data) != expected.get("bytes") or digest(data) != expected.get("sha256")):
        raise CompositionRefused("Parent metadata differs from its manifest hash or size.")
    return data


def _object(data: bytes) -> dict[str, Any]:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise CompositionRefused("Expected a JSON metadata object.")
    return value


def _arrays(target: str, shape: tuple[int, ...], bits: int | None) -> dict[str, tuple[str, tuple[int, ...]]]:
    if bits is None:
        return {target: ("F32" if len(shape) == 1 else "F16", shape)}
    rows, columns = shape
    prefix = target.removesuffix(".weight")
    return {target: ("U32", (rows, columns * bits // 32)),
            prefix + ".scales": ("F16", (rows, columns // 32)),
            prefix + ".biases": ("F16", (rows, columns // 32))}


def _header(file: Path, expected: dict[str, tuple[str, tuple[int, ...]]], entry: dict[str, Any]) -> int:
    if file.stat().st_size != entry.get("bytes") or not HASH.fullmatch(str(entry.get("sha256", ""))):
        raise CompositionRefused("A parent safetensors size or declared digest is invalid.")
    with file.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise CompositionRefused("Truncated safetensors header.")
        length = struct.unpack("<Q", prefix)[0]
        if not 2 <= length <= HEADER_LIMIT:
            raise CompositionRefused("Safetensors header exceeds the bounded limit.")
        header = _object(stream.read(length))
    header.pop("__metadata__", None)
    if set(header) != set(expected):
        raise CompositionRefused("Safetensors keys differ from their complete parent index.")
    intervals = []
    for name, (dtype, shape) in expected.items():
        value = header[name]
        width = 2 if dtype == "F16" else 4
        size = math.prod(shape) * width
        offsets = value.get("data_offsets") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get("dtype") != dtype or value.get("shape") != list(shape)
                or not isinstance(offsets, list) or len(offsets) != 2
                or any(type(offset) is not int for offset in offsets)
                or offsets[0] < 0 or offsets[1] - offsets[0] != size):
            raise CompositionRefused("Safetensors shape, dtype or payload span differs from the profile.")
        intervals.append(tuple(offsets))
    at = 0
    for start, end in sorted(intervals):
        if start != at:
            raise CompositionRefused("Safetensors payload has a gap or overlap.")
        at = end
    if entry.get("tensor_data_bytes") != at or file.stat().st_size != 8 + length + at:
        raise CompositionRefused("Safetensors payload bytes differ from their manifest.")
    return at


def _parent(directory: Path, kind: str) -> dict[str, Any]:
    if not directory.is_absolute() or not directory.is_dir() or (directory / ".conversion-incomplete").exists():
        raise CompositionRefused("Use complete absolute local parent directories.")
    raw = _read_metadata(directory, "conversion-manifest.json")
    manifest = _object(raw)
    label = REPACK4_F16_CANDIDATE_LABEL if kind == "q4_repack" else AFFINE8_CANDIDATE_LABEL
    profile = "source-repack4-f16" if kind == "q4_repack" else "affine8"
    if (manifest.get("status") != "converted_candidate_unqualified" or manifest.get("candidate_label") != label
            or manifest.get("quantisation_profile") != profile or manifest.get("source_sha256") != SOURCE_SHA256
            or manifest.get("source_bytes") != SOURCE_BYTES or manifest.get("source_text_tensors") != SOURCE_TEXT_TENSORS
            or manifest.get("source_text_parameters") != SOURCE_TEXT_PARAMETERS
            or manifest.get("clinical_quality_qualified") is not False or manifest.get("lossless_conversion") is not False
            or not isinstance(manifest.get("files"), dict) or not isinstance(manifest.get("matrices"), list)
            or not isinstance(manifest.get("excluded_vision_projector_tensors"), list)
            or len(manifest["excluded_vision_projector_tensors"]) != VISION_COUNT):
        raise CompositionRefused("A parent does not retain the complete pinned source provenance/profile.")
    if kind == "q4_repack" and manifest.get("fresh_quantization_performed") is not False:
        raise CompositionRefused("Q4 parent must explicitly preserve source integers without fresh quantization.")
    files = manifest["files"]
    config = _object(_read_metadata(directory, "config.json", files.get("config.json", {})))
    if (any(config.get(key) != value for key, value in ARCHITECTURE_PINS.items())
            or config.get("orb_candidate_label") != label or config.get("orb_quantisation_profile") != profile
            or config.get("orb_clinical_quality_qualified") is not False):
        raise CompositionRefused("A parent architecture or config qualification label differs from the pin.")
    index = _object(_read_metadata(directory, "model.safetensors.index.json", files.get("model.safetensors.index.json", {})))
    expected = expected_shapes(config)
    matrices = {}
    for row in manifest["matrices"]:
        if (not isinstance(row, dict) or not isinstance(row.get("target"), str)
                or row.get("target") in matrices or row.get("target") not in expected
                or row.get("source_type") not in MATRIX_COUNTS or row.get("shape") != list(expected[row["target"]])):
            raise CompositionRefused("Parent matrix identities, shapes or source precision mapping differ.")
        if not isinstance(row.get("source"), str) or tensor_mapping(row["source"]) != (row["target"], False):
            raise CompositionRefused("Parent original GGUF names differ from the pinned text tensor mapping.")
        matrices[row["target"]] = row
    if ({kind: sum(row["source_type"] == kind for row in matrices.values()) for kind in MATRIX_COUNTS} != MATRIX_COUNTS
            or len(expected) != SOURCE_TEXT_TENSORS or len(expected) - len(matrices) != NORM_COUNT
            or sum(math.prod(shape) for shape in expected.values()) != SOURCE_TEXT_PARAMETERS):
        raise CompositionRefused("Complete parent matrix/norm counts or parameter coverage differ.")
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict):
        raise CompositionRefused("A complete parent safetensors index is required.")
    selected = {}
    all_keys: set[str] = set()
    all_files = set()
    for target, shape in expected.items():
        row = matrices.get(target)
        bits = None if row is None or (kind == "q4_repack" and row["source_type"] == 14) else 4 if kind == "q4_repack" else 8
        if row is not None and (row.get("native_bits") != bits or
                (kind == "q4_repack" and row["source_type"] == 12 and row.get("source_integer_codes_preserved") is not True)):
            raise CompositionRefused("Parent native precision or source Q4 integer guarantee changed.")
        arrays = _arrays(target, shape, bits)
        name = weight_map.get(target)
        if not isinstance(name, str) or any(weight_map.get(key) != name for key in arrays) or name in all_files:
            raise CompositionRefused("Every base tensor must own one complete distinct indexed safetensors file.")
        entry = files.get(name)
        if not isinstance(entry, dict):
            raise CompositionRefused("An indexed safetensors file is absent from its parent manifest.")
        file = _file(directory, name)
        _header(file, arrays, entry)
        selected[target] = {"source_file": file, "source_name": name, "entry": entry, "arrays": arrays,
                            "source_type": None if row is None else row["source_type"], "matrix": row, "bits": bits}
        all_keys.update(arrays)
        all_files.add(name)
    if set(weight_map) != all_keys or {name for name in files if name.endswith(".safetensors")} != all_files:
        raise CompositionRefused("Parent index/manifest has missing or unaccounted weight keys/files.")
    return {"path": directory, "manifest": manifest, "manifest_sha256": digest(raw), "config": config,
            "selected": selected, "files": files}


def plan_composition(q4_parent: Path, affine8_parent: Path) -> dict[str, Any]:
    parents = {"q4_repack": _parent(q4_parent, "q4_repack"), "affine8": _parent(affine8_parent, "affine8")}
    repack, affine = parents.values()
    ignored = {"quantization", "quantization_config", "orb_candidate_label", "orb_quantisation_profile"}
    if ({k: v for k, v in repack["config"].items() if k not in ignored} !=
            {k: v for k, v in affine["config"].items() if k not in ignored}
            or repack["manifest"]["excluded_vision_projector_tensors"] != affine["manifest"]["excluded_vision_projector_tensors"]):
        raise CompositionRefused("Parent architecture or excluded vision mapping differs.")
    common = list(COMMON_METADATA)
    if "special_tokens_map.json" in repack["files"] or "special_tokens_map.json" in affine["files"]:
        common.append("special_tokens_map.json")
    for name in common:
        entries = [parent["files"].get(name) for parent in parents.values()]
        if any(not isinstance(entry, dict) for entry in entries) or entries[0] != entries[1]:
            raise CompositionRefused("Parent tokenizer/template/generation metadata identities differ.")
        for parent in parents.values():
            _read_metadata(parent["path"], name, parent["files"][name])
    selected = []
    for index, target in enumerate(sorted(repack["selected"]), 1):
        a, b = repack["selected"][target], affine["selected"].get(target)
        if b is None or a["source_type"] != b["source_type"] or (a["matrix"] is not None and a["matrix"]["source"] != b["matrix"]["source"]):
            raise CompositionRefused("Parents refer to different original GGUF tensor identities.")
        origin = "affine8" if a["source_type"] == 14 else "q4_repack"
        chosen = copy.copy(parents[origin]["selected"][target])
        chosen.update(target=target, origin=origin, output_name=f"model-{index:05d}-of-{SOURCE_TEXT_TENSORS:05d}.safetensors")
        selected.append(chosen)
    return {"parents": parents, "selected": selected, "common_metadata": common,
            "payload_bytes": sum(row["entry"]["tensor_data_bytes"] for row in selected)}


def _nocache(stream: BinaryIO) -> None:
    if sys.platform == "darwin":
        import fcntl
        fcntl.fcntl(stream.fileno(), 48, 1)  # Darwin F_NOCACHE, per descriptor.


def _verify_stream(source: Path, expected: dict[str, Any], destination: Path | None = None) -> None:
    hasher = hashlib.sha256()
    count = 0
    with source.open("rb") as stream:
        _nocache(stream)
        before = os.fstat(stream.fileno())
        out = destination.open("xb") if destination is not None else None
        try:
            if out is not None:
                _nocache(out)
            while block := stream.read(IO_CHUNK_BYTES):
                count += len(block)
                hasher.update(block)
                if out is not None:
                    out.write(block)
        finally:
            if out is not None:
                out.close()
        after = os.fstat(stream.fileno())
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or count != expected["bytes"] or hasher.hexdigest() != expected["sha256"]):
        raise CompositionRefused("Selected source bytes changed or failed the full uncached digest check.")


def _transfer(source: Path, destination: Path, expected: dict[str, Any]) -> str:
    try:
        os.link(source, destination)
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EROFS}:
            raise
        _verify_stream(source, expected, destination)
        return "verified_stream_copy_after_hardlink_unavailable"
    _verify_stream(destination, expected)
    return "verified_hardlink_parent_inode_never_modified_or_chmoded"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def compose(plan: dict[str, Any], output: Path) -> dict[str, Any]:
    if not output.is_absolute() or output.exists() or not output.parent.is_dir():
        raise CompositionRefused("Execution requires a new absolute output directory with an existing parent.")
    for parent in plan["parents"].values():
        if digest(_read_metadata(parent["path"], "conversion-manifest.json")) != parent["manifest_sha256"]:
            raise CompositionRefused("A parent manifest changed after planning.")
    output.mkdir(mode=0o700)
    marker = output / ".conversion-incomplete"
    marker.write_text("Composition incomplete: do not load.\n")
    repack = plan["parents"]["q4_repack"]
    manifest = {"status": "composing_candidate", "candidate_label": CANDIDATE_LABEL, "quantisation_profile": PROFILE,
                "source_sha256": SOURCE_SHA256, "source_bytes": SOURCE_BYTES,
                "source_text_tensors": SOURCE_TEXT_TENSORS, "source_text_parameters": SOURCE_TEXT_PARAMETERS,
                "clinical_quality_qualified": False, "lossless_conversion": False,
                "scope": "Sage text only; vision and imaging are not supported",
                "quantization": "Selection only: source Q4 integer-repack files and source-derived Q6 affine8 files; no additional quantization or rerounding during composition. Parent FP16 parameter rounding and Q6 requantization remain; not lossless.",
                "excluded_vision_projector_tensors": repack["manifest"]["excluded_vision_projector_tensors"],
                "expected_weight_data_bytes_excluding_headers": plan["payload_bytes"],
                "composer_sha256": digest(Path(__file__).read_bytes()), "files": {}, "file_origins": {}, "matrices": [],
                "source_parents": {name: {"path": str(parent["path"]), "manifest_sha256": parent["manifest_sha256"],
                                         "candidate_label": parent["manifest"]["candidate_label"]}
                                   for name, parent in plan["parents"].items()},
                "selected_weight_hashes_verified": False, "unselected_parent_weight_hashes_verified": False,
                "fresh_quantization_during_composition": False, "additional_rounding_during_composition": False,
                "hardlink_semantics": "Hardlinks share parent inodes; composer never writes or chmods them. Verify all candidate file hashes before loading, and keep parent/candidate weights unchanged.",
                "quality_gates": {"token_ids_vs_ollama": "pending", "clinical_regressions": "pending", "long_context": "pending", "load_test": "pending"}}
    _write_json(output / "conversion-manifest.json", manifest)
    try:
        config = copy.deepcopy(repack["config"])
        config.update(orb_candidate_label=CANDIDATE_LABEL, orb_quantisation_profile=PROFILE, orb_clinical_quality_qualified=False)
        config.pop("quantization_config", None)
        quantization = {"bits": 4, "group_size": 32, "mode": "affine"}
        weight_map = {}
        for row in plan["selected"]:
            name = row["output_name"]
            method = _transfer(row["source_file"], output / name, row["entry"])
            manifest["files"][name] = copy.copy(row["entry"])
            manifest["file_origins"][name] = {"parent": row["origin"], "source_file": row["source_name"], "transfer": method}
            weight_map.update({key: name for key in row["arrays"]})
            if row["matrix"] is not None:
                quantization[row["target"].removesuffix(".weight")] = {"bits": row["bits"], "group_size": 32, "mode": "affine"}
                record = copy.deepcopy(row["matrix"])
                record.update(selected_from_parent=row["origin"], additional_rerounding_during_composition=False)
                manifest["matrices"].append(record)
        if len(manifest["files"]) != SOURCE_TEXT_TENSORS:
            raise CompositionRefused("Composition did not select every text tensor file.")
        for name in plan["common_metadata"]:
            _verify_stream(_file(repack["path"], name), repack["files"][name], output / name)
        config["quantization"] = quantization
        _write_json(output / "model.safetensors.index.json", {"metadata": {"total_size": plan["payload_bytes"]}, "weight_map": weight_map})
        # A loadable config is created only after all selected file digests pass.
        _write_json(output / "config.json", config)
        for parent in plan["parents"].values():
            if digest(_read_metadata(parent["path"], "conversion-manifest.json")) != parent["manifest_sha256"]:
                raise CompositionRefused("A parent manifest changed during composition.")
        for file in sorted(output.iterdir()):
            if file.name not in manifest["files"] and file.name not in {marker.name, "conversion-manifest.json"}:
                data = _read_metadata(output, file.name)
                manifest["files"][file.name] = {"sha256": digest(data), "bytes": len(data)}
        manifest.update(status="converted_candidate_unqualified", selected_weight_hashes_verified=True)
        _write_json(output / "conversion-manifest.json", manifest)
        marker.unlink()
        return manifest
    except BaseException as error:
        manifest.update(status="failed_incomplete_do_not_load", error_type=type(error).__name__)
        _write_json(output / "conversion-manifest.json", manifest)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q4-repack-parent", type=Path, required=True)
    parser.add_argument("--affine8-parent", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-composed-unqualified-candidate", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.execute and not args.acknowledge_composed_unqualified_candidate:
            raise CompositionRefused("Execution requires explicit acknowledgement of the unqualified composed candidate.")
        plan = plan_composition(args.q4_repack_parent, args.affine8_parent)
        if args.execute:
            manifest = compose(plan, args.output_dir)
            print(json.dumps({"status": manifest["status"], "candidate_label": CANDIDATE_LABEL, "output_dir": str(args.output_dir)}))
        else:
            print(json.dumps({"status": "dry_plan_no_weight_payload_hashes_or_output", "candidate_label": CANDIDATE_LABEL,
                              "selected_tensor_files": len(plan["selected"]), "payload_bytes": plan["payload_bytes"],
                              "selected_weight_hashes_verified": False, "clinical_quality_qualified": False}))
        return 0
    except (CompositionRefused, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "composition_refused_or_incomplete", "error_type": type(error).__name__, "reason": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
