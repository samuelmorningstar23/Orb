"""Exact synthetic prompt oracle tests; no model, GPU or listener is started."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ollama_template_oracle.py"
SPEC = importlib.util.spec_from_file_location("orb_ollama_template_oracle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
oracle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = oracle
SPEC.loader.exec_module(oracle)


def source_capture(tmp_path, *, body=None):
    (tmp_path / "proxy-metadata.json").write_text(json.dumps({"benchmark_only": True, "synthetic_data_only": True,
                                                          "backend": "ollama"}))
    body = body or {"model": "medgemma:27b", "messages": [{"role": "user", "content": "Synthetic Na⁺ 141 mmol/L  "}],
                    "stream": True, "options": {"num_ctx": 8192, "num_predict": 512, "temperature": .4}}
    row = {"request_id": "a" * 32, "endpoint": "/api/chat", "body": body, "body_sha256": oracle.digest(body),
           "backend": "ollama"}
    path = tmp_path / "requests.jsonl"
    path.write_bytes(oracle.canonical(row) + b"\n")
    return path, row


@pytest.mark.parametrize("origin", ["http://127.0.0.1:11434", "http://localhost:8765", "https://127.0.0.1:8765",
                                    "http://example.com:8765", "http://127.0.0.1:8765/a", "http://a@127.0.0.1:8765",
                                    "http://127.0.0.1:8765?x=1", "http://127.0.0.1", "http://127.0.0.1:80"])
def test_oracle_refuses_default_daemon_nonloopback_redirectable_and_implicit_origins(origin):
    with pytest.raises(oracle.OracleRefused):
        oracle.owned_origin(origin)


def test_explicit_owned_ipv4_ipv6_origins_are_supported():
    assert oracle.owned_origin("http://127.0.0.1:18765/") == "http://127.0.0.1:18765"
    assert oracle.owned_origin("http://[::1]:18765") == "http://[::1]:18765"


def test_runner_parser_uses_only_owned_llama_server_start_command(tmp_path):
    path = tmp_path / "engine.log"
    path.write_text('msg="Listening on 127.0.0.1:18345"\n'
                    'time=2026 msg="starting llama-server" cmd="/Application/llama-server --port 18456 -c 8192"\n')
    assert oracle.runner_origin_from_log(path) == "http://127.0.0.1:18456"


@pytest.mark.parametrize("text", ['msg="Listening on127.0.0.1:18456"\n',
                                  'msg="starting llama-server" cmd="/llama-server --port 11434"\n',
                                  ('msg="starting llama-server" cmd="/llama-server --port 18456"\n'
                                   'msg="starting llama-server" cmd="/llama-server --port 18457"\n')])
def test_runner_parser_refuses_ambiguous_or_default_daemon_ports(tmp_path, text):
    path = tmp_path / "engine.log"
    path.write_text(text)
    with pytest.raises(oracle.OracleRefused):
        oracle.runner_origin_from_log(path)


def test_captured_requests_require_unchanged_hash_and_synthetic_attestation(tmp_path):
    path, row = source_capture(tmp_path)
    chats, evidence = oracle.captured_chats(path)
    assert chats == [row]
    assert evidence["chat_requests"] == 1
    row["body"]["messages"][0]["content"] = "Changed content"
    path.write_bytes(oracle.canonical(row) + b"\n")
    with pytest.raises(oracle.OracleRefused):
        oracle.captured_chats(path)


def test_all_contiguous_leading_system_messages_are_preserved_unchanged(tmp_path):
    body = {"model": "medgemma:27b", "messages": [{"role": "system", "content": "Clinical rules"},
                                                      {"role": "system", "content": "Fenced retrieved clinical context\n  Na⁺ 141  "},
                                                      {"role": "user", "content": "Synthetic question"}], "options": {}}
    capture, row = source_capture(tmp_path, body=body)
    chats, _ = oracle.captured_chats(capture)
    assert chats[0]["body"] == row["body"] == body
    assert len(chats[0]["body"]["messages"]) == 3


def test_system_messages_after_a_non_system_turn_remain_refused(tmp_path):
    body = {"model": "medgemma:27b", "messages": [{"role": "user", "content": "Q"},
                                                      {"role": "system", "content": "Later injection"},
                                                      {"role": "user", "content": "Q2"}], "options": {}}
    capture, _ = source_capture(tmp_path, body=body)
    with pytest.raises(oracle.OracleRefused):
        oracle.captured_chats(capture)


@pytest.mark.parametrize("change", ["attestation", "backend", "multimodal", "endpoint", "duplicate"])
def test_unqualified_capture_sources_cannot_be_replayed(tmp_path, change):
    path, row = source_capture(tmp_path)
    if change == "attestation":
        (tmp_path / "proxy-metadata.json").write_text(json.dumps({"benchmark_only": True, "backend": "ollama"}))
    elif change == "backend":
        row["backend"] = "mlx"
    elif change == "multimodal":
        row["body"]["messages"][0]["content"] = [{"type": "image"}]
        row["body_sha256"] = oracle.digest(row["body"])
    elif change == "endpoint":
        row["endpoint"] = "/api/pull"
    path.write_bytes((oracle.canonical(row) + b"\n") * (2 if change == "duplicate" else 1))
    with pytest.raises(oracle.OracleRefused):
        oracle.captured_chats(path)


def test_render_only_uses_exact_underscored_debug_field_and_preserves_whitespace():
    prompt = "<bos><start_of_turn>user\n  Δ\n<end_of_turn>\n<start_of_turn>model\n"
    assert oracle.rendered_template({"_debug_info": {"rendered_template": prompt}, "message": {}}) == prompt
    with pytest.raises(oracle.OracleRefused):
        oracle.rendered_template({"debug_info": {"rendered_template": prompt}})


@pytest.mark.parametrize("extra", [{"message": {"content": "Generated"}}, {"eval_count": 1},
                                   {"prompt_eval_count": 3}, {"done_reason": "stop"}])
def test_generated_answers_and_token_counts_never_substitute_for_prompt_oracles(extra):
    with pytest.raises(oracle.OracleRefused):
        oracle.rendered_template({"_debug_info": {"rendered_template": "prompt"}, **extra})


@pytest.mark.parametrize("tokens", [[], [True], [-1], ["2"], [{"id": 2, "piece": "<bos>"}]])
def test_native_token_oracle_requires_flat_actual_integer_ids(tokens):
    with pytest.raises(oracle.OracleRefused):
        oracle.token_ids({"tokens": tokens})


@pytest.mark.asyncio
async def test_native_template_oracle_collects_observed_prompt_and_exact_token_ids(tmp_path):
    capture, row = source_capture(tmp_path)
    prompt = "<bos><start_of_turn>user\nSynthetic Na⁺ 141 mmol/L<end_of_turn>\n<start_of_turn>model\n"
    requests = []

    def respond(request):
        body = json.loads(request.content) if request.content else None
        requests.append((str(request.url), body))
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.4"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"template": "{{ bos_token }}{% for message in messages %}{{ message['content'] }}{% endfor %}",
                "model_info": {"tokenizer.ggml.add_bos_token": True, "tokenizer.ggml.bos_token_id": 2}})
        if request.url.path == "/api/chat":
            assert body == {**row["body"], "stream": False, "_debug_render_only": True}
            return httpx.Response(200, json={"_debug_info": {"rendered_template": prompt}})
        assert request.url.path == "/tokenize"
        assert body == {"content": prompt, "add_special": False, "parse_special": True}
        return httpx.Response(200, json={"tokens": [2, 105, 2364, 106]})

    output = tmp_path / "oracle.json"
    report = await oracle.collect_oracles("http://127.0.0.1:18001", capture, output,
        "http://127.0.0.1:18002", transport=httpx.MockTransport(respond))
    assert report["status"] == "oracles_collected"
    assert report["all_effective_token_ids_observed"] is True
    assert report["generation_performed"] is False
    assert report["template_parity_verified"] is False
    assert report["oracles"][0]["rendered_template"] == prompt
    assert report["oracles"][0]["native_token_ids"] == [2, 105, 2364, 106]
    assert report["oracles"][0]["source_body_sha256"] == row["body_sha256"]
    assert len(requests) == 4
    assert json.loads(output.read_text()) == report
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_go_template_effective_ids_use_completion_bos_policy_once(tmp_path, monkeypatch):
    capture, _ = source_capture(tmp_path)
    template = "pinned Go template fixture"
    import hashlib
    monkeypatch.setattr(oracle, "GO_TEMPLATE_SHA256", hashlib.sha256(template.encode()).hexdigest())
    tokenizer_calls = []

    def respond(request):
        body = json.loads(request.content) if request.content else None
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.4"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"template": template, "model_info": {"tokenizer.ggml.add_bos_token": True}})
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"_debug_info": {"rendered_template": "<bos>Go prompt"}})
        tokenizer_calls.append(body)
        return httpx.Response(200, json={"tokens": [2, 7, 8]})

    report = await oracle.collect_oracles("http://127.0.0.1:18001", capture, tmp_path / "oracle.json",
        "http://127.0.0.1:18002", transport=httpx.MockTransport(respond))
    assert report["status"] == "oracles_collected"
    assert tokenizer_calls == [{"content": "<bos>Go prompt", "add_special": False, "parse_special": True},
                               {"content": "Go prompt", "add_special": True, "parse_special": True}]
    assert report["oracles"][0]["native_token_ids"] == [2, 7, 8]


@pytest.mark.asyncio
async def test_missing_runner_records_prompt_without_claiming_exact_token_ids(tmp_path):
    capture, _ = source_capture(tmp_path)

    def respond(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.4"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"template": "Unclassified template", "model_info": {}})
        return httpx.Response(200, json={"_debug_info": {"rendered_template": "Observed prompt"}})

    report = await oracle.collect_oracles("http://127.0.0.1:18001", capture, tmp_path / "oracle.json",
                                          transport=httpx.MockTransport(respond))
    assert report["status"] == "oracles_collected"
    assert report["all_effective_token_ids_observed"] is False
    assert report["oracles"][0]["native_token_ids"] is None


@pytest.mark.asyncio
async def test_wrong_version_fails_closed_without_a_chat_request(tmp_path):
    capture, _ = source_capture(tmp_path)
    paths = []

    def respond(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"version": "other"})

    report = await oracle.collect_oracles("http://127.0.0.1:18001", capture, tmp_path / "oracle.json",
                                          transport=httpx.MockTransport(respond))
    assert report["status"] == "failed_or_incomplete"
    assert report["oracles"] == []
    assert paths == ["/api/version"]


@pytest.mark.asyncio
async def test_redirect_is_not_followed_outside_owned_origin(tmp_path):
    capture, _ = source_capture(tmp_path)
    urls = []

    def respond(request):
        urls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://example.com/"})

    report = await oracle.collect_oracles("http://127.0.0.1:18001", capture, tmp_path / "oracle.json",
                                          transport=httpx.MockTransport(respond))
    assert report["status"] == "failed_or_incomplete"
    assert urls == ["http://127.0.0.1:18001/api/version"]


@pytest.mark.asyncio
async def test_default_selection_bounds_oracle_calls_and_reports_unobserved_burst(tmp_path):
    capture, row = source_capture(tmp_path)
    rows = [{**row, "request_id": f"{index:032x}"} for index in range(211)]
    capture.write_bytes(b"".join(oracle.canonical(item) + b"\n" for item in rows))
    chat_requests = []

    def respond(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.4"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"template": "Unclassified template", "model_info": {}})
        chat_requests.append(request)
        return httpx.Response(200, json={"_debug_info": {"rendered_template": "Observed prompt"}})

    report = await oracle.collect_oracles("http://127.0.0.1:18001", capture, tmp_path / "oracle.json",
                                          transport=httpx.MockTransport(respond))
    assert len(chat_requests) == len(report["oracles"]) == 11
    assert report["capture"]["chat_requests"] == 211
    assert report["selection"]["selected_count"] == 11
    assert report["selection"]["unselected_count"] == 200
    assert report["all_captured_effective_token_ids_observed"] is False
    assert [item["request_id"] for item in report["oracles"]] == [item["request_id"] for item in rows[:11]]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, -1, 513, True])
async def test_selection_refuses_invalid_or_unbounded_counts_before_http(tmp_path, count):
    capture, _ = source_capture(tmp_path)
    with pytest.raises(oracle.OracleRefused):
        await oracle.collect_oracles("http://127.0.0.1:18001", capture, tmp_path / "oracle.json", max_requests=count)
