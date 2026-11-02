"""Owned proxy protocol tests: fake upstream only, no model or listeners."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mlx_ollama_benchmark_proxy as proxy


def chat_body(*, stream: bool = True) -> dict[str, Any]:
    return {"model": proxy.MODEL, "stream": stream, "keep_alive": "-1",
            "messages": [{"role": "system", "content": "Synthetic benchmark only."},
                         {"role": "user", "content": "What is recorded in the invented chart?"}],
            "options": {"num_ctx": 8192, "num_predict": 2048, "temperature": .4,
                        "repeat_penalty": 1.15, "repeat_last_n": 256}}


def policy_file(tmp_path: Path, **overrides: Any) -> Path:
    source = tmp_path / "evidence.txt"
    if not source.exists():
        source.write_text("Fake evidence for protocol tests, not clinical qualification.")
    evidence = {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    policy = {"mlx_lm_version": "0.32.2", "source_ollama_weight_digest": proxy.WEIGHT_DIGEST,
              "conversion_label": "Synthetic test-only converted checkpoint",
              "sampling_parity_verified": True, "template_parity_verified": True,
              "sampling_evidence": evidence, "template_evidence": evidence,
              "context_policy": "reject_input_plus_output_over_8192", **overrides}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    return path


def app_for(tmp_path: Path, handler: Any, *, backend: str = "mlx",
            prompt: proxy.PromptProof | None = None, **kwargs: Any) -> Any:
    return proxy.create_app(backend=backend, upstream="http://127.0.0.1:18080",
        capture_dir=tmp_path / "new-captures", synthetic_data_only=True,
        transport=httpx.MockTransport(handler),
        mlx_policy_path=policy_file(tmp_path) if backend == "mlx" else None,
        prompt_encoder=lambda _messages: prompt or proxy.PromptProof("A verified fake prompt", [1, 2, 3]),
        **kwargs)


def frame(data: object) -> bytes:
    return b"data: " + (data.encode() if isinstance(data, str) else json.dumps(data).encode()) + b"\n\n"


def upstream_stream(*, reason: str = "stop", prompt_tokens: int = 3,
                    acknowledge: bool = True, include_usage: bool = True) -> bytes:
    result = frame({"choices": [{"delta": {"role": "assistant", "content": "NOACT|A synthetic answer has enough readable words."},
                                  "finish_reason": None}]})
    result += frame({"choices": [{"delta": {"role": "assistant"}, "finish_reason": reason}]})
    if include_usage:
        result += frame({"choices": [], "usage": {"prompt_tokens": prompt_tokens,
            "completion_tokens": 8, "prompt_tokens_details": {"cached_tokens": 1}}})
    if acknowledge:
        result += frame("[DONE]")
    return result


def content_rows(response: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line]


@pytest.mark.parametrize("origin", ["http://127.0.0.1:11434", "http://hospital.example:8000",
    "https://127.0.0.1:8080", "http://localhost:8080", "http://127.0.0.1:8080/path",
    "http://name:secret@127.0.0.1:8080", "http://127.0.0.1:8080?token=secret"])
def test_only_owned_literal_loopback_origin_is_accepted(origin: str) -> None:
    with pytest.raises(proxy.ProxyRefused, match="loopback"):
        proxy.local_origin(origin)


def test_attestation_and_new_private_capture_directory_are_mandatory(tmp_path: Path) -> None:
    directory = tmp_path / "untouched"
    with pytest.raises(proxy.ProxyRefused, match="attestation"):
        proxy.create_app(backend="ollama", upstream="http://127.0.0.1:18080",
            capture_dir=directory, synthetic_data_only=False)
    assert not directory.exists()
    directory.mkdir()
    with pytest.raises(proxy.ProxyRefused, match="new absolute"):
        proxy.create_app(backend="ollama", upstream="http://127.0.0.1:18080",
            capture_dir=directory, synthetic_data_only=True)


def test_native_sampling_mismatch_requires_explicit_unqualified_diagnostic(tmp_path: Path) -> None:
    path = policy_file(tmp_path, sampling_parity_verified=False)
    with pytest.raises(proxy.ProxyRefused, match="explicit diagnostic"):
        proxy.load_policy(path)
    result = proxy.load_policy(path, allow_unqualified=True)
    assert result["sampling_parity_verified"] is False
    app = proxy.create_app(backend="mlx", upstream="http://127.0.0.1:18080",
        capture_dir=tmp_path / "captures", synthetic_data_only=True, mlx_policy_path=path,
        prompt_encoder=lambda _: proxy.PromptProof("fake", [1]), allow_unqualified_mlx=True)
    assert app.state.metadata["unqualified_mlx_diagnostic"] is True
    assert app.state.metadata["clinical_validation"] == "not_established_by_this_proxy"


def test_changed_local_parity_evidence_fails_closed(tmp_path: Path) -> None:
    path = policy_file(tmp_path)
    (tmp_path / "evidence.txt").write_text("changed")
    with pytest.raises(proxy.ProxyRefused, match="absent or changed"):
        proxy.load_policy(path)


def test_real_local_fast_tokenizer_returns_ids_despite_transformers_five_default_dict(tmp_path: Path) -> None:
    from tokenizers import Tokenizer, models
    from transformers import PreTrainedTokenizerFast

    backend = Tokenizer(models.WordLevel({"<unk>": 0, "<bos>": 1, "Hello": 2}, unk_token="<unk>"))
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="<unk>", bos_token="<bos>",
                                       chat_template="{{ bos_token }}{{ messages[0]['content'] }}")
    tokenizer.save_pretrained(str(tmp_path))
    encode = proxy.tokenizer_encoder(tmp_path)
    proof = encode([{"role": "user", "content": "Hello"}])
    assert proof.rendered_prompt == "<bos>Hello"
    assert proof.token_ids == tokenizer.encode("<bos>Hello", add_special_tokens=False)
    assert type(proof.token_ids) is list and all(type(token) is int for token in proof.token_ids)


@pytest.mark.parametrize("bad_ids", [{"input_ids": [1, 2]}, [[1, 2]], [True], [-1], []])
def test_local_tokenizer_encoder_refuses_nonflat_or_invalid_ids(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_ids: Any) -> None:
    from transformers import AutoTokenizer

    class BadTokenizer:
        chat_template = "synthetic"

        def apply_chat_template(self, _messages: Any, *, tokenize: bool, **kwargs: Any) -> Any:
            if not tokenize:
                return "Synthetic prompt"
            assert kwargs["return_dict"] is False
            return bad_ids

    monkeypatch.setattr(AutoTokenizer, "from_pretrained", lambda *_args, **_kwargs: BadTokenizer())
    encode = proxy.tokenizer_encoder(tmp_path)
    with pytest.raises(proxy.ProxyRefused, match="flat sequence"):
        encode([{"role": "user", "content": "Synthetic input"}])


def test_mapping_preserves_scope_budgets_sampling_stop_and_repeat_window() -> None:
    body = chat_body()
    mapped = proxy.mlx_body(body)
    assert mapped["messages"] == body["messages"] and mapped["model"] == "default_model"
    assert mapped["temperature"] == .4 and mapped["max_tokens"] == 2048
    assert mapped["top_k"] == 40 and mapped["top_p"] == .9 and mapped["min_p"] == 0
    assert mapped["repetition_penalty"] == 1.15 and mapped["repetition_context_size"] == 256
    assert mapped["stop"] == ["<end_of_turn>"] and mapped["num_draft_tokens"] == 0
    assert mapped["stream_options"] == {"include_usage": True}
    for budget in (512, 768, 1024, 2048):
        body["options"]["num_predict"] = budget
        body["options"]["temperature"] = .2
        assert proxy.mlx_body(body)["max_tokens"] == budget
        assert proxy.mlx_body(body)["temperature"] == .2


def test_actual_sage_default_one_hour_retention_is_accepted_without_body_mutation() -> None:
    body = chat_body()
    body["keep_alive"] = "1h"  # server/config.py's actual default.
    before = json.loads(json.dumps(body))
    assert proxy.validate_request(body) is body
    assert proxy.mlx_body(body)["messages"] == before["messages"]
    assert body == before and body["keep_alive"] == "1h"


def test_actual_sage_rules_and_fenced_context_system_messages_are_preserved() -> None:
    body = chat_body()
    body["messages"].insert(1, {"role": "system", "content": "<<<CLINICAL_CONTEXT>>>Synthetic record<<<END_CLINICAL_CONTEXT>>>"})
    body["keep_alive"] = "1h"
    before = json.loads(json.dumps(body))
    assert proxy.validate_request(body) is body
    assert proxy.mlx_body(body)["messages"] == before["messages"]
    assert body == before
    body["messages"].append({"role": "system", "content": "Late system messages remain unsupported"})
    with pytest.raises(proxy.ProxyRefused):
        proxy.validate_request(body)


@pytest.mark.parametrize("options", [{"num_ctx": 4096}, {"num_predict": 100}, {"num_predict": True},
    {"repeat_penalty": 1}, {"repeat_last_n": 20}, {"top_k": 0}, {"top_p": 1},
    {"min_p": .1}, {"stop": ["different"]}, {"num_batch": 256}, {"temperature": float("nan")}])
def test_unsupported_or_weaker_parameters_are_never_silently_ignored(options: dict[str, Any]) -> None:
    body = chat_body()
    body["options"].update(options)
    with pytest.raises(proxy.ProxyRefused):
        proxy.mlx_body(body)


def test_ollama_mode_forwards_original_bytes_and_captures_stable_replay_format(tmp_path: Path) -> None:
    async def exercise() -> None:
        seen = []
        raw = json.dumps(chat_body(), indent=3).encode()
        answer = b'{"model":"medgemma:27b","message":{"content":"unchanged"},"done":true}\n'

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.content, dict(request.headers)))
            return httpx.Response(200, content=answer, headers={"content-type": "application/x-ndjson"})

        app = app_for(tmp_path, handler, backend="ollama")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
            response = await client.post("/api/chat", content=raw, headers={"Authorization": "not-forwarded"})
        assert response.content == answer and seen[0][:2] == ("/api/chat", raw)
        assert "authorization" not in seen[0][2]
        directory = tmp_path / "new-captures"
        rows = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
        assert len(rows) == 1
        row = rows[0]
        assert row["endpoint"] == "/api/chat" and row["body"] == json.loads(raw)
        assert row["body_sha256"] == proxy.digest(json.loads(raw)) and row["backend"] == "ollama"
        captured = json.loads((directory / (row["request_id"] + "-request.json")).read_text())
        assert captured["raw_request_sha256"] == hashlib.sha256(raw).hexdigest()
        assert captured["prompt_token_ids"] == [1, 2, 3]
        assert directory.stat().st_mode & 0o077 == 0
        assert (directory / "requests.jsonl").stat().st_mode & 0o077 == 0
    asyncio.run(exercise())


def test_mlx_stream_has_truthful_terminal_usage_and_residency_only_after_completion(tmp_path: Path) -> None:
    async def exercise() -> None:
        seen = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, content=upstream_stream(), headers={"content-type": "text/event-stream"})

        app = app_for(tmp_path, handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
            assert (await client.get("/api/ps")).json() == {"models": []}
            response = await client.post("/api/chat", json=chat_body())
            resident = (await client.get("/api/ps")).json()["models"]
            version = (await client.get("/api/version")).json()
        rows = content_rows(response)
        assert seen[0][0] == "/v1/chat/completions" and seen[0][1] == proxy.mlx_body(chat_body())
        assert rows[0]["message"]["content"].startswith("NOACT|") and not rows[0]["done"]
        assert rows[-1]["done"] and rows[-1]["done_reason"] == "stop"
        assert rows[-1]["prompt_eval_count"] == 3 and rows[-1]["eval_count"] == 8
        assert rows[-1]["prompt_eval_cached_count"] == 1 and rows[-1]["backend"] == "mlx"
        assert resident[0]["name"] == proxy.MODEL and resident[0]["conversion_label"]
        assert resident[0]["digest"].startswith("mlx-candidate:")
        assert resident[0]["source_ollama_weight_digest"] == proxy.WEIGHT_DIGEST
        assert version["version"] == "mlx-lm-0.32.2-benchmark-proxy"
    asyncio.run(exercise())


@pytest.mark.parametrize("case", ["missing_done", "missing_usage", "bad_tokenizer_count", "invalid_json", "tools", "late_event"])
def test_partial_invalid_or_unmatched_mlx_stream_never_emits_success_terminal(tmp_path: Path, case: str) -> None:
    async def exercise() -> None:
        value = upstream_stream(acknowledge=case != "missing_done", include_usage=case != "missing_usage",
                                prompt_tokens=4 if case == "bad_tokenizer_count" else 3)
        if case == "invalid_json":
            value = frame("not JSON")
        elif case == "tools":
            value = frame({"choices": [{"delta": {"tool_calls": []}, "finish_reason": None}]})
        elif case == "late_event":
            value += frame({"choices": [{"delta": {"content": "late"}, "finish_reason": None}]})
        app = app_for(tmp_path, lambda _: httpx.Response(200, content=value))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
            rows = content_rows(await client.post("/api/chat", json=chat_body()))
        assert any(row.get("error") for row in rows)
        assert not any(row.get("done") is True for row in rows)
        assert app.state.loaded is False
    asyncio.run(exercise())


def test_length_exhaustion_is_forwarded_as_length_for_existing_provider_to_reject(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = app_for(tmp_path, lambda _: httpx.Response(200, content=upstream_stream(reason="length")))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
            rows = content_rows(await client.post("/api/chat", json=chat_body()))
        assert rows[-1]["done"] is True and rows[-1]["done_reason"] == "length"
        assert not any(row.get("done_reason") == "stop" for row in rows)
    asyncio.run(exercise())


def test_context_limit_rejects_before_capture_or_upstream_without_shortening_budget(tmp_path: Path) -> None:
    async def exercise() -> None:
        seen = []
        app = app_for(tmp_path, lambda request: seen.append(request),
                      prompt=proxy.PromptProof("large synthetic prompt", [1] * 7000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
            response = await client.post("/api/chat", json=chat_body())
        assert response.status_code == 400 and not seen
        assert not (tmp_path / "new-captures" / "requests.jsonl").exists()
    asyncio.run(exercise())


@pytest.mark.parametrize("field,value", [("model", "medgemma:4b"), ("tools", []),
    ("messages", [{"role": "user", "content": [{"type": "image_url", "image_url": "http://remote"}]}])])
def test_model_downgrade_external_images_and_tools_are_refused(tmp_path: Path, field: str, value: Any) -> None:
    async def exercise() -> None:
        seen = []
        app = app_for(tmp_path, lambda request: seen.append(request))
        body = chat_body()
        body[field] = value
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
            response = await client.post("/api/chat", json=body)
        assert response.status_code == 400 and not seen
    asyncio.run(exercise())


def test_nonloopback_clients_are_refused_even_if_listener_is_misconfigured(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = app_for(tmp_path, lambda _: httpx.Response(200))
        transport = httpx.ASGITransport(app, client=("192.0.2.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
            assert (await client.post("/api/chat", json=chat_body())).status_code == 403
    asyncio.run(exercise())


def test_nonstream_synthetic_warmup_is_limited_and_length_remains_visible(tmp_path: Path) -> None:
    async def exercise() -> None:
        seen = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"choices": [{"text": "x", "finish_reason": "length"}],
                                           "usage": {"prompt_tokens": 4, "completion_tokens": 1}})

        app = app_for(tmp_path, handler)
        body = {"model": proxy.MODEL, "prompt": proxy.WARMUP, "stream": False,
                "keep_alive": "30m", "options": {"num_ctx": 8192, "num_predict": 1}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client:
            response = await client.post("/api/generate", json=body)
            body["prompt"] = "Arbitrary clinical prompt is not the warmup"
            rejected = await client.post("/api/generate", json=body)
        assert seen[0][0] == "/v1/completions" and seen[0][1]["max_tokens"] == 1
        assert response.json()["done_reason"] == "length" and response.json()["response"] == "x"
        assert app.state.loaded is True and rejected.status_code == 400 and len(seen) == 1
    asyncio.run(exercise())
