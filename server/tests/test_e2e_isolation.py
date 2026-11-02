"""The browser harness must never inherit appliance state or copy clinical files."""
import importlib.util
import json
from pathlib import Path

import pytest


_path = Path(__file__).resolve().parents[2] / "scripts/e2e_server.py"
_spec = importlib.util.spec_from_file_location("orb_e2e_server", _path)
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)


def test_environment_replaces_appliance_paths_secrets_and_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("ORB_DB_PATH", "/real/appliance.db")
    monkeypatch.setenv("ORB_UPLOAD_ENCRYPTION_KEY", "appliance-secret")
    monkeypatch.setenv("ORB_JWT_SECRET", "appliance-secret")
    monkeypatch.setenv("ORB_WARD_RECORD", "1")
    monkeypatch.setenv("ORB_LAB", "1")
    monkeypatch.setenv("PYTHONPATH", "/real/appliance")
    for key in ("TRANSFORMERS_CACHE", "PYTORCH_TRANSFORMERS_CACHE", "PYTORCH_PRETRAINED_BERT_CACHE",
                "SENTENCE_TRANSFORMERS_HOME", "TORCH_HOME", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, "/real/models")
    env = e2e.isolated_environment(tmp_path)
    assert env["ORB_DB_PATH"] == str(tmp_path / "demo.db")
    assert env["ORB_JWT_SECRET"] != "appliance-secret"
    assert "ORB_UPLOAD_ENCRYPTION_KEY" not in env
    assert "ORB_WARD_RECORD" not in env
    assert "ORB_LAB" not in env
    assert "PYTHONPATH" not in env
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["ORB_OLLAMA_BASE_URL"] == "http://127.0.0.1:9"
    assert env["ORB_WHISPER_DAEMON_ENABLED"] == "0"
    assert env["ORB_WHISPER_SOCKET"] == f"ipc://{tmp_path / 'whisper.sock'}"
    for key in ("HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE", "PYTORCH_TRANSFORMERS_CACHE",
                "PYTORCH_PRETRAINED_BERT_CACHE", "SENTENCE_TRANSFORMERS_HOME", "TORCH_HOME",
                "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert e2e.isolated_environment(tmp_path)["ORB_JWT_SECRET"] != env["ORB_JWT_SECRET"]


def test_copy_excludes_runtime_state_models_settings_and_symlinks(monkeypatch, tmp_path):
    source = tmp_path / "repo"
    paths = ["server/main.py", "server/routes/patients.py", "server/models/user.py",
             "server/models/medgemma/config.json", "server/models/medgemma/weights.safetensors",
             "server/.env", "server/orb.db", "server/uploads/patient.json",
             "server/downtime_exports/patient.json", "server/site_config/site.toml",
             "server/site_config/seasonal_calendar.json", "server/ops/patient.json"]
    for name in paths:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture")
    (source / "server/symlink.py").symlink_to(source / "server/main.py")
    paths.append("server/symlink.py")
    (source / "dist").mkdir()
    (source / "dist/index.html").write_text("built application")
    monkeypatch.setattr(e2e.subprocess, "check_output", lambda *_a, **_kw: "\0".join(paths).encode())
    target = tmp_path / "disposable"
    e2e.copy_application(source, target)
    copied = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert copied == {"server/main.py", "server/routes/patients.py", "server/models/user.py",
                      "server/site_config/seasonal_calendar.json", "dist/index.html"}


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:11434", "http://example.org:11434", "http://127.0.0.2:11434",
    "http://127.0.0.1", "http://127.0.0.1:11434/api", "http://127.0.0.1:11434?x=1",
    "http://127.0.0.1:11434#x", "http://user@127.0.0.1:11434",
    "http://127.0.0.1:65536", "file:///tmp/model", "http://127.0.0.1:11434@elsewhere:80",
])
def test_inference_option_refuses_non_loopback_origins_credentials_and_url_suffixes(url):
    with pytest.raises(e2e.argparse.ArgumentTypeError):
        e2e.loopback_inference_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434/", "http://localhost:11434"])
def test_inference_option_is_explicit_careful_and_still_disposable(monkeypatch, tmp_path, url):
    monkeypatch.setenv("ORB_LLM_MODEL", "unsafe-inherited-model")
    monkeypatch.setenv("ORB_FAST_MODE_SAFETY_FLOOR", "0")
    env = e2e.local_inference_environment(tmp_path, url)
    assert env["ORB_OLLAMA_BASE_URL"] == url.rstrip("/")
    assert env["ORB_LLM_MODEL"] == env["ORB_AMBIENT_LLM_MODEL"] == "medgemma:27b"
    assert env["ORB_MODEL_FALLBACK_ENABLED"] == "false"
    assert env["ORB_THINKING_DEFAULT"] == "on"
    assert env["ORB_FAST_MODE_SAFETY_FLOOR"] == "1"
    assert env["ORB_BACKGROUND_MODELS_ENABLED"] == "0"
    assert env["ORB_SAGE_PREWARM"] == env["ORB_SAGE_AMBIENT"] == "false"
    assert env["ORB_SAGE_ASSESSMENT_ENABLED"] == "false"
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    assert Path(env["ORB_DB_PATH"]).is_relative_to(tmp_path)
    assert Path(env["HF_HOME"]).is_relative_to(tmp_path)
    assert e2e.loopback_inference_url("http://[::1]:11434/") == "http://[::1]:11434"
    # The normal no-inference journeys retain their existing default.
    assert e2e.isolated_environment(tmp_path)["ORB_OLLAMA_BASE_URL"] == "http://127.0.0.1:9"


def test_installed_model_probe_is_read_only_and_bypasses_inherited_proxies(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            assert limit == 1_048_577
            return json.dumps({"models": [{"name": "medgemma:27b", "digest": "synthetic-digest",
                                           "size": 123, "private_extra": "not exported"}]}).encode()

    class Opener:
        def open(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    def build(handler):
        assert handler.proxies == {}
        return Opener()

    monkeypatch.setattr(e2e, "build_opener", build)
    assert e2e.installed_local_model("http://127.0.0.1:11434") == {
        "name": "medgemma:27b", "digest": "synthetic-digest", "size": 123,
    }
    assert calls == [("http://127.0.0.1:11434/api/tags", {"timeout": 5})]


@pytest.mark.parametrize("payload", [{"models": []}, {"models": [{"name": "medgemma:4b"}]},
                                      {"models": [{"name": "medgemma:27b"}]}, [], {"models": None}])
def test_inference_probe_never_downloads_a_missing_or_unidentified_model(monkeypatch, payload):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(payload).encode()

    class Opener:
        def open(self, url, **_kwargs):
            assert url.endswith("/api/tags")
            return Response()

    monkeypatch.setattr(e2e, "build_opener", lambda _handler: Opener())
    with pytest.raises(ValueError):
        e2e.installed_local_model("http://127.0.0.1:11434")


def test_installed_snapshots_are_explicit_read_sources_and_caches_remain_disposable(tmp_path):
    embedder = tmp_path / "installed-embedder"
    reranker = tmp_path / "installed-reranker"
    for snapshot in (embedder, reranker):
        snapshot.mkdir()
        (snapshot / "config.json").write_text('{"model_type":"synthetic-test-only"}')
    runtime = tmp_path / "disposable"
    env = e2e.local_inference_environment(runtime, "http://127.0.0.1:11434", str(embedder), str(reranker))
    assert env["ORB_EMBEDDING_MODEL"] == str(embedder)
    assert env["SAGE_RERANK_MODEL"] == str(reranker)
    assert env["ORB_KNOWLEDGE_INPROCESS"] == "0"
    assert "ORB_SKIP_KNOWLEDGE_BASE" not in env
    assert Path(env["HF_HOME"]).is_relative_to(runtime)
    assert Path(env["TORCH_HOME"]).is_relative_to(runtime)
    assert sorted(p.name for p in embedder.iterdir()) == ["config.json"]
    with pytest.raises(ValueError, match="both"):
        e2e.local_inference_environment(runtime, "http://127.0.0.1:11434", str(embedder))
    for value in ("BAAI/bge-m3", str(tmp_path / "missing"), str(tmp_path)):
        with pytest.raises(e2e.argparse.ArgumentTypeError):
            e2e.local_snapshot_path(value)


def test_chart_prefix_warm_is_an_explicit_separate_qualification_option(tmp_path):
    env = e2e.local_inference_environment(tmp_path, "http://127.0.0.1:11434", chart_prewarm=True)
    assert env["ORB_SAGE_PREWARM"] == "true"
    assert env["ORB_BACKGROUND_MODELS_ENABLED"] == "0"
    assert env["ORB_SAGE_AMBIENT"] == "false"
    assert env["ORB_FAST_MODE_SAFETY_FLOOR"] == "1"
    assert env["ORB_LLM_MODEL"] == "medgemma:27b"


def test_chart_prefix_warm_cannot_enable_inference_without_explicit_opt_in(monkeypatch, capsys, tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/index.html").write_text("synthetic app")
    monkeypatch.setattr(e2e, "ROOT", tmp_path)
    monkeypatch.setattr(e2e.sys, "argv", ["e2e_server.py", "--chart-prewarm"])
    with pytest.raises(SystemExit) as rejected:
        e2e.main()
    assert rejected.value.code == 2
    assert "--chart-prewarm requires --local-inference-url" in capsys.readouterr().err


def test_opt_in_initializes_real_corpus_inside_the_same_owned_checkout(monkeypatch, tmp_path):
    root = tmp_path / "source"
    (root / "dist").mkdir(parents=True)
    (root / "dist/index.html").write_text("synthetic app")
    snapshot = tmp_path / "installed-snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}")
    monkeypatch.setattr(e2e, "ROOT", root)
    monkeypatch.setattr(e2e, "copy_application", lambda _root, target: (target / "server").mkdir())
    monkeypatch.setattr(e2e.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(e2e, "installed_local_model", lambda _url: {"name": "medgemma:27b", "digest": "synthetic"})
    monkeypatch.setattr(e2e.sys, "argv", ["e2e_server.py", "--local-inference-url", "http://127.0.0.1:11434",
                                        "--embedding-model", str(snapshot), "--reranker-model", str(snapshot)])
    calls = []

    def owned(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["env"]["ORB_LLM_MODEL"] == "medgemma:27b"
        assert kwargs["env"]["ORB_BACKGROUND_MODELS_ENABLED"] == "0"
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert Path(kwargs["env"]["ORB_DB_PATH"]).parent == kwargs["cwd"].parent
        return 0

    monkeypatch.setattr(e2e, "run_owned_child", owned)
    assert e2e.main() == 0
    assert len(calls) == 3
    assert calls[0][0][-1] == "scripts/reset_db_and_seed.py"
    assert calls[1][0][-1] == e2e._INITIALIZE_KNOWLEDGE
    assert calls[1][1]["check"] is True
    assert "uvicorn" in calls[2][0]
    assert all(not kwargs["cwd"].parent.exists() for _command, kwargs in calls)
