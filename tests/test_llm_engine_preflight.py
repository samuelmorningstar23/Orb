"""Preflight tests use synthetic directories and never load a GPU or model."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("scripts.llm_engine_preflight", SCRIPTS / "llm_engine_preflight.py")
assert SPEC is not None and SPEC.loader is not None
preflight: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


def _string(value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return struct.pack("<Q", len(data)) + data


def _metadata(key: str, kind: int, data: bytes) -> bytes:
    return _string(key) + struct.pack("<I", kind) + data


def _tensor(name: str, shape: tuple[int, ...], kind: int, offset: int) -> bytes:
    return (_string(name) + struct.pack("<I", len(shape))
            + b"".join(struct.pack("<Q", item) for item in shape)
            + struct.pack("<IQ", kind, offset))


def _gguf(*, tensors: list[bytes] | None = None, entries: list[bytes] | None = None,
          payload_bytes: int = 512, version: int = 3) -> tuple[bytes, int, int]:
    tensors = [_tensor("blk.0.q.weight", (256,), 12, 0)] if tensors is None else tensors
    entries = [] if entries is None else entries
    header = (b"GGUF" + struct.pack("<IQQ", version, len(tensors), len(entries))
              + b"".join(entries) + b"".join(tensors))
    data_start = (len(header) + 31) // 32 * 32
    return header + b"\0" * (data_start - len(header)) + b"W" * payload_bytes, len(header), data_start


class HeaderOnlyStream(io.BytesIO):
    """Fail the test if parsing ever reads even one model payload byte."""
    def __init__(self, data: bytes, data_start: int):
        super().__init__(data)
        self.data_start = data_start
        self.maximum_read_end = 0

    def read(self, size: int | None = -1) -> bytes:
        assert size is not None and size >= 0, "Unbounded file read attempted"
        assert self.tell() + size <= self.data_start, "Model payload was read"
        self.maximum_read_end = max(self.maximum_read_end, self.tell() + size)
        return super().read(size)


def test_mixed_quantization_counts_exactly_without_reading_weight_payload() -> None:
    entries = [_metadata("general.architecture", 8, _string("gemma3")),
               _metadata("general.parameter_count", 10, struct.pack("<Q", 772)),
               _metadata("tokenizer.ggml.tokens", 9,
                         struct.pack("<IQ", 8, 3) + _string("a") + _string("b") + _string("c"))]
    tensors = [_tensor("blk.0.q.weight", (256, 2), 12, 0),
               _tensor("blk.0.v.weight", (256,), 14, 288),
               _tensor("v.norm.weight", (2,), 0, 512),
               _tensor("embed.weight", (2,), 1, 544)]
    data, index_end, data_start = _gguf(tensors=tensors, entries=entries, payload_bytes=560)
    stream = HeaderOnlyStream(data, data_start)
    result = preflight.parse_gguf(stream, len(data))
    assert result["tensor_count"] == 4
    assert result["total_tensor_elements"] == 772
    assert result["non_vision_prefix_tensor_elements"] == 770
    assert result["all_weights_fp16_expansion_bytes"] == 1544
    assert result["tensor_types"] == {
        "12": {"name": "Q4_K", "tensors": 1, "elements": 512, "stored_bytes": 288},
        "14": {"name": "Q6_K", "tensors": 1, "elements": 256, "stored_bytes": 210},
        "0": {"name": "F32", "tensors": 1, "elements": 2, "stored_bytes": 8},
        "1": {"name": "F16", "tensors": 1, "elements": 2, "stored_bytes": 4}}
    assert result["metadata"]["tokenizer.ggml.tokens"] == {"element_type": 8, "count": 3}
    assert result["header_and_tensor_index_sha256"] == hashlib.sha256(data[:index_end]).hexdigest()
    assert result["tensor_index_end_bytes"] == stream.maximum_read_end == index_end
    assert result["weight_payload_read"] is False and result["full_weight_sha256"] is None


@pytest.mark.parametrize("data", [b"", b"GGUF", b"NOPE" + b"\0" * 20,
    b"GGUF" + struct.pack("<IQQ", 2, 1, 0),
    b"GGUF" + struct.pack("<IQQ", 3, 0, 0),
    b"GGUF" + struct.pack("<IQQ", 3, 100_001, 0),
    b"GGUF" + struct.pack("<IQQ", 3, 1, 4097)])
def test_invalid_or_truncated_header_is_rejected(data: bytes) -> None:
    with pytest.raises(preflight.PreflightRefused):
        preflight.parse_gguf(io.BytesIO(data), len(data))


@pytest.mark.parametrize("entry", [
    _metadata("unknown", 999, b""),
    _metadata("bad-array", 9, struct.pack("<IQ", 4, 1_000_001)),
    _metadata("nested-array", 9, struct.pack("<IQ", 9, 1)),
    _metadata("huge-string", 8, struct.pack("<Q", 8 * 1024 * 1024 + 1)),
    _metadata("general.architecture", 8, _string(b"\xff")),
    _metadata("general.alignment", 4, struct.pack("<I", 3)),
    _metadata("general.alignment", 4, struct.pack("<I", 0)),
    _metadata("general.alignment", 4, struct.pack("<I", 8192)),
    _metadata("general.alignment", 7, struct.pack("<?", True)),
    _metadata("general.alignment", 8, _string("32")),
    _metadata("general.value", 6, struct.pack("<f", float("nan"))),
])
def test_malformed_metadata_sizes_types_and_alignment_fail_closed(entry: bytes) -> None:
    data, _, _ = _gguf(entries=[entry])
    with pytest.raises(preflight.PreflightRefused):
        preflight.parse_gguf(io.BytesIO(data), len(data))


def test_total_array_and_index_limits_are_enforced_before_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    data, _, _ = _gguf(entries=[_metadata("a", 9, struct.pack("<IQ", 0, 4) + b"1234"),
                               _metadata("b", 9, struct.pack("<IQ", 0, 4) + b"5678")])
    monkeypatch.setattr(preflight, "MAX_TOTAL_ARRAY_ELEMENTS", 7)
    with pytest.raises(preflight.PreflightRefused, match="element limit"):
        preflight.parse_gguf(io.BytesIO(data), len(data))
    monkeypatch.setattr(preflight, "MAX_INDEX_BYTES", 24)
    with pytest.raises(preflight.PreflightRefused, match="index limit"):
        preflight.parse_gguf(io.BytesIO(data), len(data))


def test_duplicate_keys_and_oversized_names_are_rejected() -> None:
    duplicate = _metadata("general.architecture", 8, _string("gemma3"))
    data, _, _ = _gguf(entries=[duplicate, duplicate])
    with pytest.raises(preflight.PreflightRefused, match="Duplicate"):
        preflight.parse_gguf(io.BytesIO(data), len(data))
    data, _, _ = _gguf(tensors=[_tensor("x" * 513, (256,), 12, 0)])
    with pytest.raises(preflight.PreflightRefused, match="string"):
        preflight.parse_gguf(io.BytesIO(data), len(data))


@pytest.mark.parametrize("tensor", [
    _tensor("bad", (), 12, 0), _tensor("bad", (1, 1, 1, 1, 1), 0, 0),
    _tensor("bad", (0,), 0, 0), _tensor("bad", (1 << 32,), 0, 0),
    _tensor("bad", (1 << 21, 1 << 21), 0, 0),
    _tensor("bad", (256,), 999, 0), _tensor("bad", (255,), 12, 0),
    _tensor("bad", (256,), 12, 1), _tensor("bad", (256,), 12, 512),
    _tensor("", (256,), 12, 0),
])
def test_invalid_tensor_geometry_type_offset_and_payload_range_are_rejected(tensor: bytes) -> None:
    data, _, _ = _gguf(tensors=[tensor])
    with pytest.raises(preflight.PreflightRefused):
        preflight.parse_gguf(io.BytesIO(data), len(data))


@pytest.mark.parametrize("second", [_tensor("second", (256,), 12, 128),
                                     _tensor("first", (256,), 12, 160)])
def test_overlapping_payloads_or_duplicate_tensor_names_are_rejected(second: bytes) -> None:
    data, _, _ = _gguf(tensors=[_tensor("first", (256,), 12, 0), second])
    with pytest.raises(preflight.PreflightRefused):
        preflight.parse_gguf(io.BytesIO(data), len(data))


def test_source_fingerprint_is_not_claimed_as_weight_hash(tmp_path: Path) -> None:
    data, _, _ = _gguf()
    path = tmp_path / "synthetic.gguf"
    path.write_bytes(data)
    before = path.stat()
    result = preflight.inspect_checkpoint(path)
    assert result["manifest_pin_verified"] is False
    assert result["pinned_ollama_manifest"] is None
    assert result["gguf"]["full_weight_sha256"] is None
    assert result["file_stat_fingerprint"]["size_bytes"] == len(data)
    assert "no manifest pin" in result["identity_assurance"]
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(preflight.PreflightRefused, match="absolute"):
        preflight.inspect_checkpoint(Path("relative.gguf"))


def test_change_during_inspection_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data, _, _ = _gguf()
    path = tmp_path / "synthetic.gguf"
    path.write_bytes(data)
    original = preflight.parse_gguf

    def inspect_then_change(stream: Any, file_size: int) -> dict[str, Any]:
        result = original(stream, file_size)
        with path.open("ab") as output:
            output.write(b"changed")
        return result

    monkeypatch.setattr(preflight, "parse_gguf", inspect_then_change)
    with pytest.raises(preflight.PreflightRefused, match="changed"):
        preflight.inspect_checkpoint(path)


def test_manifest_pin_means_manifest_and_size_only_not_computed_full_hash(tmp_path: Path) -> None:
    models = tmp_path / "models"
    (models / "blobs").mkdir(parents=True)
    data, _, _ = _gguf()
    blob = models / "blobs" / preflight.MODEL_DIGEST.replace(":", "-")
    blob.write_bytes(data)
    config_digest = "sha256:" + "c" * 64
    (models / "blobs" / config_digest.replace(":", "-")).write_text(json.dumps({
        "model_family": "gemma3", "model_type": "27.4B", "file_type": "Q4_K_M"}))
    manifest = models / "manifests/registry.ollama.ai/library/medgemma/27b"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"config": {"digest": config_digest}, "layers": [{
        "mediaType": "application/vnd.ollama.image.model", "digest": preflight.MODEL_DIGEST,
        "size": len(data)}]}))
    result = preflight.inspect_checkpoint(blob, models)
    assert result["manifest_pin_verified"] is True
    assert result["pinned_ollama_manifest"]["model_digest"] == preflight.MODEL_DIGEST
    assert "full weight hash NOT verified" in result["identity_assurance"]
    alternate = tmp_path / "same-bytes.gguf"
    alternate.write_bytes(data)
    with pytest.raises(preflight.PreflightRefused, match="manifest blob"):
        preflight.inspect_checkpoint(alternate, models)


def test_package_inventory_reads_metadata_without_importing_gpu_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def version(name: str) -> str:
        calls.append(name)
        if name in {"mlx", "mlx-lm", "mlx-metal", "mlx-vlm"}:
            raise preflight.metadata.PackageNotFoundError(name)
        return "synthetic-version"

    monkeypatch.setattr(preflight.metadata, "version", version)
    before = {name for name in sys.modules if name.startswith(("mlx", "torch"))}
    result = preflight.installed_packages()
    assert result["mlx"] is None and result["torch"] == "synthetic-version"
    assert set(calls) == {"mlx", "mlx-lm", "mlx-metal", "mlx-vlm", "transformers", "torch", "safetensors"}
    assert before == {name for name in sys.modules if name.startswith(("mlx", "torch"))}


def test_same_memory_guard_comparison_does_not_qualify_mlx_or_sla(monkeypatch: pytest.MonkeyPatch) -> None:
    checkpoint = {"file_stat_fingerprint": {"size_bytes": 17_397_211_520},
                  "gguf": {"all_weights_fp16_expansion_bytes": 54_864_813_280}}
    monkeypatch.setattr(preflight, "inspect_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(preflight, "installed_packages", lambda: {"mlx": "0.32.3", "mlx-lm": None})
    memory_type = preflight.inspect_memory.__globals__["MemorySnapshot"]
    monkeypatch.setattr(preflight, "inspect_memory", lambda: memory_type(64 * 1024 ** 3, 75, 48 * 1024 ** 3, "synthetic"))
    report = preflight.build_report(Path("/synthetic/model"))
    first, second, third = report["memory_budgets"]
    assert first["existing_ollama_required_available_bytes"] == 17_397_211_520 + 24 * 1024 ** 3
    assert first["existing_ollama_guard_passed"] is second["existing_ollama_guard_passed"] is True
    assert third["existing_ollama_guard_passed"] is False
    assert all(row["fp16_same_guard_passed"] is False for row in report["memory_budgets"])
    assert report["mlx_identical_checkpoint_ready"] is report["clinical_quality_qualified"] is False
    assert report["sla_benchmark_performed"] is report["models_loaded"] is False
    assert "mlx-lm_not_installed_in_selected_interpreter" in report["mlx_block_reasons"]
    assert "fp16_expansion_fails_existing_single_slot_headroom_guard" in report["mlx_block_reasons"]


def test_new_report_is_workspace_only_private_and_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "REPO_ROOT", tmp_path / "workspace")
    output = tmp_path / "workspace/results/new.json"
    preflight.write_new_report(output, {"offline": True})
    assert json.loads(output.read_text()) == {"offline": True}
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        preflight.write_new_report(output, {"overwrite": True})
    assert json.loads(output.read_text()) == {"offline": True}
    with pytest.raises(preflight.PreflightRefused, match="workspace"):
        preflight.write_new_report(tmp_path / "outside.json", {})
    with pytest.raises(preflight.PreflightRefused, match="absolute"):
        preflight.write_new_report(Path("relative.json"), {})
    with pytest.raises(preflight.PreflightRefused, match="JSON"):
        preflight.write_new_report(tmp_path / "workspace/report.txt", {})


def test_cli_refusal_does_not_write_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(preflight, "REPO_ROOT", tmp_path)
    output = tmp_path / "refused.json"
    assert preflight.main(["--model-path", "relative.gguf", "--report", str(output)]) == 2
    assert not output.exists()
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["status"] == "refused" and refusal["models_loaded"] is False
