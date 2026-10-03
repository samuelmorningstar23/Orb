"""Sage lane hygiene (F138, 2026-10-03).

Five small defects in how Orb's model lanes are wired, each pinned here:

  1. AN OPERATOR UNLOAD DID NOT STICK. `keep_warm_loop` treated a tier an
     administrator unloaded as "evicted by Ollama" and re-loaded it within one
     300 s interval, so the memory the confirmation promised came straight back.
     The unload route now holds the tier (`main._OPERATOR_UNLOADED`), the load
     route releases it, and both warming paths skip a held tier.
  2. TWO LANES BYPASSED THE ROUTER. Bridge and the discharge draft asked Ollama
     for LLM_MODEL by name, which silently re-loads an unloaded careful model.
     Both now go through `select_chat_model`, and neither drafts on the smaller
     model: Bridge gives a fixed reply, discharge stores recorded data only.
  3. THE UNLOAD CONFIRMATION SAID FEATURES "STOP WORKING". Sage answers on the
     smaller model (labelled) and some lanes load the model straight back.
  4. FORMAT RULES IN THE WRONG PLACE. The 4B obeys a format rule only as the
     last thing it reads (measured 2026-09-07), so the ambient classifier and
     Scribe correction prompts now END with theirs, and ambient asks Ollama for
     JSON.
  5. VERB-SUBSTRING ROUTING. The ambient sanitizer and the handoff dispatcher
     now read one exact table (`sage_handoff_service.ACTION_ROUTES`). The
     dispatch side, with a DB, is pinned in tests/test_handoff.py.
"""
import ast
import asyncio
import inspect
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402
from services import model_router  # noqa: E402
from services.model_router import ModelChoice  # noqa: E402


@pytest.fixture(autouse=True)
def _no_held_tiers():
    """The hold is process state; never leak it into another test."""
    main._OPERATOR_UNLOADED.clear()
    yield
    main._OPERATOR_UNLOADED.clear()


# ── 1. an operator unload sticks ─────────────────────────────────────────────

def test_background_warming_skips_a_tier_an_administrator_unloaded():
    main._OPERATOR_UNLOADED.add("medgemma:27b")
    assert main.models_to_warm(["medgemma:27b", "medgemma:4b"], frozenset()) == ["medgemma:4b"]


def test_background_warming_still_reloads_a_tier_ollama_dropped():
    """The hold is the ONLY new exclusion: an eviction nobody asked for is
    still re-warmed, which is what keep-warm exists for."""
    assert main.models_to_warm(["medgemma:27b", "medgemma:4b"], frozenset({"medgemma:4b"})) == ["medgemma:27b"]
    assert main.models_to_warm(["medgemma:27b", "medgemma:27b"]) == ["medgemma:27b"]


def _nested_function(outer, name):
    tree = ast.parse(textwrap.dedent(inspect.getsource(outer)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found inside {outer.__name__}")


@pytest.mark.parametrize("fn", ["keep_warm_loop", "warmup_models"])
def test_both_warming_paths_consult_the_hold(fn):
    """Both are closures inside `lifespan`, so this reads their source: each
    must decide what to load through `models_to_warm`, or an unloaded tier
    comes back on the next pass (keep-warm) or at the end of a long queue
    wait (boot warmup)."""
    node = _nested_function(main.lifespan, fn)
    calls = {n.func.id for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "models_to_warm" in calls, f"{fn} warms without checking the operator hold"


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_ollama(monkeypatch, *, post_status=200, resident=()):
    import httpx as _httpx

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            return _FakeResp(post_status)

        async def get(self, url, **kw):
            return _FakeResp(200, {"models": [{"name": m} for m in resident]})

    monkeypatch.setattr(_httpx, "AsyncClient", _Client)


@pytest.fixture
def admin_client():
    import uuid
    from fastapi.testclient import TestClient
    from auth import create_token
    from database import db_session

    uid = f"HYG{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Hygiene Admin', 'admin', 'x', 'Administration')", (uid,))
    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {create_token(uid, 'admin')}"})
    return client


def test_unload_holds_the_tier_and_load_releases_it(monkeypatch, admin_client):
    from config import LLM_MODEL
    _fake_ollama(monkeypatch)

    r = admin_client.post("/api/health/models/unload", json={"model": LLM_MODEL})
    assert r.status_code == 200, r.text
    assert LLM_MODEL in main._OPERATOR_UNLOADED
    assert LLM_MODEL not in main.models_to_warm([LLM_MODEL])
    health = admin_client.get("/api/health/models").json()
    assert next(m for m in health["models"] if m["model"] == LLM_MODEL)["held"] is True

    r = admin_client.post("/api/health/models/load", json={"model": LLM_MODEL})
    assert r.status_code == 200, r.text
    assert LLM_MODEL not in main._OPERATOR_UNLOADED
    health = admin_client.get("/api/health/models").json()
    assert next(m for m in health["models"] if m["model"] == LLM_MODEL)["held"] is False


def test_an_unload_ollama_refused_is_not_held(monkeypatch, admin_client):
    """The tier is still resident, so holding it would misreport it."""
    from config import LLM_MODEL
    _fake_ollama(monkeypatch, post_status=500)

    r = admin_client.post("/api/health/models/unload", json={"model": LLM_MODEL})
    assert r.status_code == 502
    assert LLM_MODEL not in main._OPERATOR_UNLOADED


# ── 3. the confirmation says what really happens ─────────────────────────────

def _two_tiers(monkeypatch, *, fallback=True):
    import config
    monkeypatch.setattr(config, "LLM_MODEL", "medgemma:27b")
    monkeypatch.setattr(config, "AMBIENT_LLM_MODEL", "medgemma:4b")
    monkeypatch.setattr(config, "VISION_MODEL", "medgemma:27b")
    monkeypatch.setattr(config, "THINKING_FAST_MODEL", "medgemma:4b")
    monkeypatch.setattr(config, "THINKING_FAST_MODE_ENABLED", True)
    monkeypatch.setattr(config, "MODEL_FALLBACK_ENABLED", fallback)
    monkeypatch.setattr(config, "SCRIBE", {
        "draft": {"model": "medgemma:27b", "provider": "ollama"},
        "correction": {"enabled": True, "model": "medgemma:4b", "provider": "ollama"},
        "verify": {"enabled": False, "model": "medgemma:4b", "provider": "ollama"},
    })


def test_unloading_the_careful_model_says_sage_falls_back_and_who_reloads_it(monkeypatch):
    _two_tiers(monkeypatch)
    impacts = main._model_impacts("medgemma:27b")
    text = " ".join(impacts)
    assert "smaller model" in text
    assert "Lens image analysis and Scribe note structuring load it again on first use" in text
    assert "stop working" not in text


def test_unloading_the_small_model_names_the_lanes_that_load_it_back(monkeypatch):
    _two_tiers(monkeypatch)
    text = " ".join(main._model_impacts("medgemma:4b"))
    assert "Scribe transcript correction and Sage fast mode load it again" in text
    assert "Scribe note checking" not in text, "a disabled stage does not reload anything"
    assert "smaller model" not in text


def test_with_fallback_off_the_careful_model_is_reloaded_not_replaced(monkeypatch):
    _two_tiers(monkeypatch, fallback=False)
    text = " ".join(main._model_impacts("medgemma:27b"))
    assert "smaller model" not in text
    assert "Sage, Bridge and discharge summaries" in text


def test_no_impact_line_uses_an_em_dash(monkeypatch):
    _two_tiers(monkeypatch)
    for name in ("medgemma:27b", "medgemma:4b"):
        for line in main._model_impacts(name):
            assert "\u2014" not in line, line


# ── 2. Bridge and discharge go through the router ────────────────────────────

def _route_to(monkeypatch, choice: ModelChoice):
    async def _select(preferred=None, fallback=None):
        return choice
    monkeypatch.setattr(model_router, "select_chat_model", _select)


def _no_ollama(monkeypatch):
    from services import ollama_gateway

    def _boom(*a, **kw):
        raise AssertionError("the model was called while the careful tier is unloaded")
    monkeypatch.setattr(ollama_gateway, "post", _boom)
    monkeypatch.setattr(ollama_gateway, "stream", _boom)


def test_bridge_does_not_answer_patients_on_the_smaller_model(monkeypatch):
    from services import bridge_service
    _route_to(monkeypatch, ModelChoice("medgemma:4b", True, "medgemma:27b"))
    _no_ollama(monkeypatch)

    async def _collect():
        return [c async for c in bridge_service.answer_patient_question(
            "When can I go home?", {"patient_name": "Test", "status": "stable"})]

    chunks = asyncio.run(_collect())
    assert chunks == [bridge_service.ASSISTANT_UNAVAILABLE_RESPONSE]
    assert "nurse" in chunks[0]


def test_bridge_asks_for_the_model_the_router_chose(monkeypatch):
    import contextlib
    from services import bridge_service, ollama_gateway
    from services.llm_queue_service import llm_queue
    _route_to(monkeypatch, ModelChoice("medgemma:27b", False, "medgemma:27b"))
    sent = []

    class _Stream:
        async def aiter_lines(self):
            yield '{"message": {"content": "Hello"}, "done": true}'

        async def aclose(self):
            return None

    @contextlib.asynccontextmanager
    async def _stream(client, path, payload, **kw):
        sent.append(payload)
        yield _Stream()

    async def _acquire(*a, **k):
        return None

    monkeypatch.setattr(ollama_gateway, "stream", _stream)
    monkeypatch.setattr(llm_queue, "acquire", _acquire)
    monkeypatch.setattr(llm_queue, "release", lambda *a, **k: None)

    async def _collect():
        return [c async for c in bridge_service.answer_patient_question(
            "What does my medicine do?", {"patient_name": "Test", "status": "stable"})]

    assert asyncio.run(_collect()) == ["Hello"]
    assert sent[0]["model"] == "medgemma:27b"


def test_discharge_draft_is_not_written_by_the_smaller_model(monkeypatch):
    """An empty draft is the route's signal to store offline_discharge_summary,
    the recorded-data-only summary that says the assistant was unavailable."""
    from services import discharge_service
    _route_to(monkeypatch, ModelChoice("medgemma:4b", True, "medgemma:27b"))
    _no_ollama(monkeypatch)
    assert asyncio.run(discharge_service.generate_discharge_summary("ANY-PATIENT")) == ""


# ── 4. format rules come last for the 4B lanes ───────────────────────────────

def test_ambient_classification_prompt_ends_with_its_format_rule():
    from services import sage_ambient_service as sas
    prompt = sas._build_prompt(
        {"sender_name": "Dr A", "sender_role": "doctor",
         "content": "Start monitoring fluid balance hourly."},
        None, {"name": "Test", "diagnosis": "Sepsis"}, [])
    assert prompt.rstrip().endswith(sas._CLASSIFY_FORMAT_RULE)
    assert prompt.index("<<<END_ROOM_DATA>>>") < prompt.index(sas._CLASSIFY_FORMAT_RULE)


def test_every_verb_the_classifier_is_told_to_use_is_routable():
    from services import sage_ambient_service as sas
    from services.sage_handoff_service import route_for_action
    for verb in ("start_medication", "transfer_to_surgery", "document"):
        assert verb in sas._CLASSIFY_FORMAT_RULE
        assert route_for_action(verb) is not None, verb


def test_ambient_classification_asks_ollama_for_json(monkeypatch):
    from services import ollama_gateway
    from services import sage_ambient_service as sas
    sent = []

    async def _not_evicted(model):
        return False

    async def _post(client, path, payload, **kw):
        sent.append(payload)
        return _FakeResp(200, {"response": "{}"})

    monkeypatch.setattr(model_router, "ambient_tier_evicted", _not_evicted)
    monkeypatch.setattr(ollama_gateway, "post", _post)
    asyncio.run(sas._ambient_ollama_call("prompt", "system"))
    assert sent and sent[0]["format"] == "json"


def test_scribe_correction_prompt_ends_with_its_format_rule():
    from services import scribe_service
    prompt = scribe_service._correction_prompt("pt c/o chest pain radiating to left arm " * 5)
    assert prompt.rstrip().endswith(scribe_service.CORRECTION_FORMAT_RULE)
    assert prompt.index("<<<END_DICTATION>>>") < prompt.index(scribe_service.CORRECTION_FORMAT_RULE)


def test_dev_scribe_stages_do_not_point_at_absent_mlx_weights():
    """They defaulted to an MLX MedGemma 1.5 path that was never downloaded."""
    import config
    if config.DEPLOYMENT_PROFILE != "dev":
        pytest.skip("dev-profile defaults")
    for stage in ("correction", "verify"):
        if os.getenv(f"ORB_SCRIBE_{stage.upper()}_MODEL") or os.getenv(f"ORB_SCRIBE_{stage.upper()}_PROVIDER"):
            continue
        assert config.SCRIBE[stage]["provider"] == "ollama"
        assert config.SCRIBE[stage]["model"] == config.THINKING_FAST_MODEL
        assert "1.5" not in config.SCRIBE[stage]["model"]


# ── 5. one exact verb table ──────────────────────────────────────────────────

@pytest.mark.parametrize("verb", [
    "start monitoring", "address concerns", "hold the line", "stop_medication",
    "transfer", "start", "add", "chart review", "discontinue", "escalate",
])
def test_the_ambient_sanitizer_drops_what_the_dispatcher_cannot_route(verb):
    from services.sage_ambient_service import _sanitize_llm_intent
    from services.sage_handoff_service import route_for_action
    assert route_for_action(verb) is None
    assert _sanitize_llm_intent({"action": verb, "target": "Something"}, 0.9) is None


@pytest.mark.parametrize("verb,module", [
    ("start_medication", "helix"), ("Start medication", "helix"), ("prescribe", "helix"),
    ("transfer_to_surgery", "admissions"), ("book OR", "admissions"),
    ("document", "scribe"), (" draft-note ", "scribe"),
])
def test_routable_verbs_survive_and_are_stored_in_the_tables_spelling(verb, module):
    from services.sage_ambient_service import _sanitize_llm_intent
    from services.sage_handoff_service import normalize_action_verb, route_for_action
    assert route_for_action(verb) == module
    intent = _sanitize_llm_intent({"action": verb, "target": "Ceftriaxone"}, 0.9)
    assert intent is not None and intent["action"] == normalize_action_verb(verb)
    assert intent["confidence"] <= 0.6


def test_the_deterministic_extractor_only_emits_table_verbs():
    """Its two verbs must stay routable, or its cards would need manual dispatch."""
    from services.sage_ambient_service import _deterministic_extract
    from services.sage_handoff_service import route_for_action
    for text in ("Start IV ceftriaxone 2 g now", "Needs an emergency laparotomy, book theatre"):
        intent = _deterministic_extract(text)
        assert intent is not None, text
        assert route_for_action(intent["action"]) is not None, intent
