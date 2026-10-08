"""The models switch: ORB_MODELS_ENABLED, and the Orb Ward Record (6 October 2026).

With the models switched off no request reaches a model from any lane: the
inference gateway and the two provider seams refuse before a request leaves
the process, Whisper, the knowledge workers, the embedder, the CfC overlay and
the voice agent do not load, the residency probe does not ask Ollama, and the
model features' routes answer 404 "Switched off on this computer". With the
switch on, everything is as it was (each refusal test has its control).

tests/test_ward_record_profile_boot.py boots the whole app both ways and
fails on any model call; this file pins each lane on its own.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import model_switch, ollama_gateway
from services.model_switch import ModelsSwitchedOff

client = TestClient(app)
SERVER = Path(__file__).resolve().parents[1]
OFF = "Switched off on this computer"


@pytest.fixture
def models_off(monkeypatch):
    monkeypatch.setattr(config, "MODELS_ENABLED", False, raising=False)


class _Client:
    """An httpx.AsyncClient stand-in that fails the test if it is used."""

    def __init__(self):
        self.calls = []

    async def post(self, *a, **k):
        self.calls.append(("post", a, k))
        raise AssertionError("a model request was made with the models switched off")

    def stream(self, *a, **k):
        self.calls.append(("stream", a, k))
        raise AssertionError("a model request was made with the models switched off")


def _audit_rows(purpose):
    with db_session() as db:
        rows = db.execute("SELECT metadata_json FROM audit_logs WHERE event_category = "
                          "'ai_inference'").fetchall()
    return [r for r in rows if json.loads(r["metadata_json"] or "{}").get("purpose") == purpose]


def test_the_switch_reads_the_models_setting_and_the_profile(monkeypatch):
    monkeypatch.setattr(config, "MODELS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    assert model_switch.models_enabled() is True
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    assert model_switch.models_enabled() is False
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    monkeypatch.setattr(config, "MODELS_ENABLED", False, raising=False)
    assert model_switch.models_enabled() is False


# ── The gateway and the seams ────────────────────────────────────────────────

def test_the_gateway_refuses_a_post_and_writes_no_audit_row(models_off):
    fake = _Client()
    with pytest.raises(ModelsSwitchedOff):
        asyncio.run(ollama_gateway.post(fake, "/api/chat", {"model": "m"},
                                        purpose="models_switch_post"))
    assert fake.calls == [] and _audit_rows("models_switch_post") == []


def test_the_gateway_refuses_a_stream(models_off):
    fake = _Client()

    async def _go():
        async with ollama_gateway.stream(fake, "/api/chat", {"model": "m"},
                                         purpose="models_switch_stream"):
            pass

    with pytest.raises(ModelsSwitchedOff):
        asyncio.run(_go())
    assert fake.calls == [] and _audit_rows("models_switch_stream") == []


def test_the_sage_chat_seam_refuses(models_off):
    from services.llm_providers import OllamaChatProvider

    async def _go():
        async for _ in OllamaChatProvider().astream_chat([{"role": "user", "content": "x"}]):
            pass

    with pytest.raises(ModelsSwitchedOff):
        asyncio.run(_go())


@pytest.mark.parametrize("cls", ["OllamaProvider", "MLXProvider"])
def test_every_scribe_provider_refuses(models_off, cls):
    from services import scribe_providers
    provider = getattr(scribe_providers, cls)()
    with pytest.raises(ModelsSwitchedOff):
        provider.generate("x")
    with pytest.raises(ModelsSwitchedOff):
        asyncio.run(provider.agenerate("x"))

    async def _stream():
        async for _ in provider.agenerate_stream("x"):
            pass

    with pytest.raises(ModelsSwitchedOff):
        asyncio.run(_stream())


# ── Whisper, the knowledge workers, the CfC overlay, the voice agent ─────────

def test_whisper_is_never_started(models_off, monkeypatch):
    from services import scribe_service
    monkeypatch.setattr(scribe_service.subprocess, "run",
                        lambda *a, **k: pytest.fail("the Whisper worker was started"))
    for call in (lambda: scribe_service.transcribe_audio("/dev/null"),
                 lambda: scribe_service._transcribe_via_daemon("/dev/null"),
                 lambda: scribe_service._transcribe_via_subprocess("/dev/null", "", {}, "en")):
        with pytest.raises(ModelsSwitchedOff):
            call()


@pytest.mark.real_knowledge_pool
def test_no_knowledge_worker_starts(models_off):
    from services import knowledge_worker as kw
    with pytest.raises(kw.KnowledgeWorkerError):
        kw._ensure_pool()
    assert kw._pool is None


def test_the_embedder_and_reranker_are_not_loaded(models_off, monkeypatch):
    from services import clinical_knowledge as ck
    monkeypatch.setattr(ck, "_embed_model", None)
    with pytest.raises(ModelsSwitchedOff):
        ck._get_embedder()
    assert ck._get_cross_encoder() is None
    monkeypatch.setattr(ck, "_corpus_fingerprint",
                        lambda: pytest.fail("the knowledge base was embedded"))
    assert ck.init_knowledge_base() is None


def test_the_cfc_overlay_is_not_run(models_off, monkeypatch):
    from services import vigil_service
    monkeypatch.setattr(vigil_service, "_load_cfc_models",
                        lambda: pytest.fail("the CfC checkpoints were loaded"))
    assert vigil_service.compute_cfc_risk("ANY") is None


def test_the_voice_agent_is_not_loaded(models_off):
    from services.voice_agent_service import get_voice_agent
    with pytest.raises(ModelsSwitchedOff):
        get_voice_agent().load()


def test_the_residency_probe_does_not_ask_ollama(models_off, monkeypatch):
    import httpx
    from services import model_router
    monkeypatch.setattr(model_router, "_ps_cache", None, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "get",
                        lambda *a, **k: pytest.fail("Ollama was asked"))
    assert asyncio.run(model_router.probe_residency()) is None


def test_config_does_not_ask_ollama_at_import_with_the_models_off():
    probe = (
        "import urllib.request, json\n"
        "calls = []\n"
        "real = urllib.request.urlopen\n"
        "urllib.request.urlopen = lambda u, *a, **k: calls.append(str(getattr(u, 'full_url', u))) or real(u, *a, **k)\n"
        "import config\n"
        "print('CALLS ' + json.dumps([c for c in calls if '11434' in c or 'api/tags' in c]))\n"
    )
    for env in ({"ORB_MODELS_ENABLED": "false"}, {"ORB_WARD_RECORD": "true"}):
        full = {k: v for k, v in os.environ.items()
                if k not in ("ORB_MODELS_ENABLED", "ORB_WARD_RECORD")}
        full.update({"ORB_DEPLOYMENT_PROFILE": "dev",
                     "ORB_OLLAMA_BASE_URL": "http://127.0.0.1:9", **env})
        out = subprocess.run([sys.executable, "-c", probe], cwd=SERVER, env=full,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        line = [ln for ln in out.stdout.splitlines() if ln.startswith("CALLS ")][-1]
        assert json.loads(line[6:]) == [], (env, line)


# ── The routes ───────────────────────────────────────────────────────────────

def _doctor():
    return {"Authorization": f"Bearer {create_token('D001', 'doctor')}"}


ROUTES_OFF = [
    ("get", "/api/sage/ward-brief"),
    ("get", "/api/sage/insights"),
    ("get", "/api/lens/analyses"),
    ("post", "/api/scribe/structure"),
    ("post", "/api/scribe/transcribe"),
    ("post", "/api/scribe/notes/1/verify"),
    ("get", "/api/scribe/asr/status"),
    ("get", "/api/scribe/daemon/status"),
    ("get", "/api/vigil/patients/ANY/timeline-projection"),
    ("get", "/api/relay/channels/ANY/sage_actions"),
    ("get", "/api/relay/channels/ANY/sage_memory"),
    ("get", "/api/relay/sage_assessments/ANY"),
    ("get", "/api/surgery/ANY/risk"),
    ("post", "/api/bridge/ask"),
    ("post", "/api/health/models/load"),
    ("post", "/api/health/models/unload"),
]


@pytest.mark.parametrize("method,path", ROUTES_OFF)
def test_the_model_routes_answer_switched_off(models_off, method, path):
    r = getattr(client, method)(path, headers=_doctor(), **({"json": {}} if method == "post" else {}))
    assert r.status_code == 404 and r.json()["detail"] == OFF, (path, r.status_code, r.text[:200])


@pytest.mark.parametrize("method,path", ROUTES_OFF[:3])
def test_the_same_routes_are_not_switched_off_with_the_models_on(monkeypatch, method, path):
    monkeypatch.setattr(config, "MODELS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    r = getattr(client, method)(path, headers=_doctor())
    assert not (r.status_code == 404 and r.json().get("detail") == OFF), (path, r.text[:200])


def test_reading_and_signing_scribe_notes_stay_open(models_off):
    r = client.get("/api/scribe/notes", headers=_doctor())
    assert r.status_code == 200, r.text


def test_the_model_status_chip_is_told_the_models_are_off(models_off):
    r = client.get("/api/health/models", headers=_doctor())
    assert r.status_code == 200
    assert r.json() == {"ollama_reachable": False, "models_enabled": False, "models": []}


def test_a_relay_message_schedules_no_ambient_reading(models_off, monkeypatch):
    from services import sage_ambient_service
    called = []

    async def _observe(*a, **k):
        called.append(a)

    monkeypatch.setattr(sage_ambient_service, "sage_ambient_observe", _observe)
    # A department room is open to a user of that department (D001 is General
    # Medicine), the same set up tests/test_relay_idempotency.py uses.
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO channels (id, name, type) VALUES "
                   "('MS-CH', 'General Medicine Models Switch Room', 'department')")
    r = client.post("/api/relay/channels/MS-CH/messages", headers=_doctor(),
                    json={"content": "hello"})
    assert r.status_code in (200, 201), r.text
    assert called == []


def test_the_live_scribe_stream_does_not_start(models_off, monkeypatch):
    from ws import scribe_stream
    sent = []

    async def _ok(_sid):
        return True

    async def _emit(event, data, to=None):
        sent.append((event, data))

    monkeypatch.setattr(scribe_stream, "_require_authenticated", _ok)
    monkeypatch.setattr(scribe_stream.sio, "emit", _emit)
    asyncio.run(scribe_stream.handle_start_session("sid-ms", {}))
    assert sent == [("scribe:error", {"error": OFF})]
    assert scribe_stream._get_session("sid-ms") is None


# ── The operations status page (review, 7 October 2026) ──────────────────────
# /api/ops/status probed Ollama's /api/ps on every read through its own urllib
# opener, which the profile probe's urlopen trip wire never saw, and then
# reported "the medical AI engine is not answering" as Urgent on a computer
# where the models are switched off on purpose.

def test_the_ops_status_page_does_not_ask_ollama_with_the_models_off(models_off, monkeypatch):
    from services import ops_health_service
    monkeypatch.setattr(ops_health_service, "_probe_models",
                        lambda *a, **k: pytest.fail("the status page asked Ollama"))
    check = ops_health_service._check_models()
    assert check["status"] == ops_health_service.OK
    assert "switched off on this computer" in check["summary"]
    models = [c for c in ops_health_service.build_status()["checks"] if c["key"] == "models"]
    assert models and models[0]["status"] == ops_health_service.OK


def test_the_ops_status_page_still_asks_ollama_with_the_models_on(monkeypatch):
    from services import ops_health_service
    monkeypatch.setattr(config, "MODELS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    asked = []
    monkeypatch.setattr(ops_health_service, "_probe_models", lambda *a, **k: asked.append(1) or {
        "reachable": False, "configured": ["m"], "loaded": [], "not_loaded": ["m"]})
    assert ops_health_service._check_models()["status"] == ops_health_service.URGENT
    assert asked == [1]


# ── The forecast LSTM (review, 7 October 2026) ──────────────────────────────
# The chart asks /api/forecast/patient/{id} on every open, and that loaded and
# ran the torch LSTM with the models switched off (only its boot warm-up was
# gated).

def test_the_forecast_lstm_is_not_loaded_with_the_models_off(models_off, monkeypatch):
    from services import forecast_service
    monkeypatch.setattr(forecast_service, "get_model",
                        lambda: pytest.fail("the forecast LSTM was loaded"))
    out = forecast_service.predict_patient_los("ANY")
    assert out["prediction_source"] == "heuristic" and out["models_enabled"] is False


def test_the_forecast_lstm_is_still_asked_with_the_models_on(monkeypatch):
    from services import forecast_service
    monkeypatch.setattr(config, "MODELS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "WARD_RECORD", False, raising=False)
    asked = []

    class _Untrained:
        trained = False

    monkeypatch.setattr(forecast_service, "get_model", lambda: asked.append(1) or _Untrained())
    monkeypatch.setattr(forecast_service, "_cache_prediction", lambda *a, **k: None)
    forecast_service.predict_patient_los("ANY")
    assert asked == [1]


# ── The discharge summary (review, 9 October 2026) ──────────────────────────
# A discharge with the models off took a queue slot and asked the gateway,
# which refused; the stored summary then said the assistant "was unavailable",
# which on an Orb Ward Record computer reads as a fault.

def test_a_discharge_drafts_nothing_and_asks_no_gateway_with_the_models_off(models_off, monkeypatch):
    from services import discharge_service
    asked = []

    async def _post(*a, **k):
        asked.append(k.get("purpose"))
        raise AssertionError("the gateway was asked with the models switched off")

    monkeypatch.setattr(ollama_gateway, "post", _post)
    assert asyncio.run(discharge_service.generate_discharge_summary("ANY")) == ""
    assert asked == []


def test_the_recorded_data_summary_says_drafting_is_switched_off(models_off):
    from services import discharge_service
    import uuid
    pid = f"MSD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, age, sex) "
                   "VALUES (?, 'Models Off Discharge', ?, 'admitted', 50, 'F')", (pid, f"MRN-{pid}"))
    text = discharge_service.offline_discharge_summary(pid)
    assert "switched off on this computer" in text
    assert "unavailable" not in text
