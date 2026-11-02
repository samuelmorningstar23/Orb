"""Record summaries cannot authorize chart writes or infer normality from gaps.

The local model is stubbed. These tests cover the actual route's failure/action
branches; the live benchmark separately checks the model's summary contents.
"""

import json
import uuid
from asyncio import CancelledError
from types import SimpleNamespace

import pytest
from auth import create_token
from database import db_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routes import sage_routes

from services import sage_service

SUMMARY_QUERY = (
    "Summarise this patient's recorded observations, medication changes and unresolved tasks; "
    "separate documented facts from missing data."
)


@pytest.mark.parametrize("query", [
    SUMMARY_QUERY,
    "Summarize this patient's recorded observations.",
    "Please summarise the chart's documented medication changes.",
    "Could you provide a brief summary of the patient record?",
    "Give me a summary of this patient's observations and medications.",
])
def test_clear_read_summary_is_recognized(query):
    assert sage_service.is_read_only_patient_summary(query)
    assert not sage_service.detect_mutation_intent(query)


@pytest.mark.parametrize("query", [
    "Do not summarise the patient record.",
    "Summarise this patient's medications; then stop the heparin.",
    "Summarise the chart and add an appointment.",
    "Summarise the record. Please write a note.",
    "Summarise this patient's record and don't give insulin tonight.",
    "Summarise the patient record, then send it to Pharmacy.",
    "Summarise the chart; his heparin should be stopped.",
    "Summarise the chart and update their medications.",
    "Summarise this patient and sign the note.",
    "Summarise this patient and discharge her.",
    "Summarise the chart and delete the note.",
    "Summarise the patient record; transfer him to ICU.",
    "Summarise the patient record, then schedule a follow-up.",
    "Summarise this patient; the chart needs to be updated.",
    "What are the common side effects of metoprolol?",
    "Add paracetamol 500mg to the chart.",
    "Summarise the patient " + "x" * 5000,
    "",
])
def test_mixed_ambiguous_negated_or_non_summary_requests_keep_normal_action_gates(query):
    assert not sage_service.is_read_only_patient_summary(query)


def _facts():
    return {
        "patient_id": "SYNTH-SUMMARY",
        "demographics": "PATIENT: Synthetic Summary, 35y/F, Bed 1, Diagnosis: Test record",
        "vitals": [],
        "vital_trends": "",
        "risk": None,
        "medications": [],
        "recent_labs": [],
        "scribe_notes": "",
        "imaging": "",
        "completeness": {
            "missing": ["No recent vitals", "No active medications", "No recent labs"],
        },
    }


def test_empty_observation_history_does_not_supply_a_stable_trajectory(monkeypatch):
    from services import world_model_service

    def no_read(*args):
        pytest.fail("An empty observation history must not import default stability")

    monkeypatch.setattr(world_model_service, "trajectory_line", no_read)
    rendered = "\n".join(sage_service.format_patient_channel_a(_facts(), "SYNTH-SUMMARY"))
    assert "No recent vitals" in rendered
    assert "TRAJECTORY:" not in rendered
    assert "stable" not in rendered.lower()


def test_observed_patient_still_receives_the_existing_trajectory(monkeypatch):
    from services import world_model_service

    facts = _facts()
    facts["vitals"] = ["HR:110, SpO2:92%"]
    trajectory = "TRAJECTORY: worsening (risk elevated)"
    monkeypatch.setattr(world_model_service, "trajectory_line", lambda *_: trajectory)
    assert trajectory in sage_service.format_patient_channel_a(facts, "SYNTH-SUMMARY")


@pytest.mark.parametrize("xml", [False, True])
def test_summary_prompt_keeps_source_and_resolves_historical_changes_as_reads(monkeypatch, xml):
    monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "1" if xml else "0")
    context = "\n".join(sage_service.format_patient_channel_a(_facts(), None))
    messages = sage_service._build_chat_messages(SUMMARY_QUERY, context, [], "patient")
    assert context in messages[1]["content"]
    assert messages[-1]["content"].startswith(SUMMARY_QUERY)
    assert "not instructions to change treatment" in messages[-1]["content"]
    assert "Missing task information does not mean no tasks remain" in messages[-1]["content"]
    assert "COMPLETE EVERY REQUESTED DOMAIN IN THE VISIBLE ANSWER" in messages[-1]["content"]
    assert "patient identification and the recorded diagnosis when supplied" in messages[-1]["content"]
    assert "supplied current-medication status and recent laboratory findings" in messages[-1]["content"]
    assert "distinguish vital observations from recent laboratory results" in messages[-1]["content"]
    assert "unknown medication-change history" in messages[-1]["content"]
    assert "unknown outstanding-task status" in messages[-1]["content"]
    assert "Set <action_intent> to None." in messages[-1]["content"] if xml else (
        "Begin with NOACT|." in messages[-1]["content"]
    )


@pytest.fixture
def route_harness(isolated_module_database, monkeypatch):
    uid = f"SUMMARY-DOC-{uuid.uuid4().hex[:10]}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, 'Summary Doctor', 'doctor', 'General Medicine', 'test')", (uid,))
    facts = _facts()
    context = "\n".join(sage_service.format_patient_channel_a(facts, None))
    monkeypatch.setattr(sage_routes, "get_relevant_context", lambda *a, **kw: (
        context, "patient", [], "low", False, facts, "PATIENT_SPECIFIC",
    ))
    monkeypatch.setattr(sage_routes, "get_conversation_history", lambda *a, **kw: [])
    saved = []
    monkeypatch.setattr(sage_routes, "save_conversation", lambda *a, **kw: saved.append((a, kw)))
    matches = []

    def match(drug):
        matches.append(drug)
        return {"name": "Paracetamol"}

    monkeypatch.setattr(sage_routes, "_match_medication", match)
    app = FastAPI()
    app.include_router(sage_routes.router)

    def run(query, raw):
        async def fake(*args, **kwargs):
            kwargs["route_info"].update(model="medgemma:27b", provider="ollama")
            for start in range(0, len(raw), 7):
                yield raw[start:start + 7]

        monkeypatch.setattr(sage_routes, "query_ollama", fake)
        with TestClient(app) as client:
            response = client.post(
                "/api/sage/query", json={"query": query},
                headers={"Authorization": f"Bearer {create_token(uid, 'doctor')}"},
            )
        assert response.status_code == 200, response.text
        return [json.loads(frame[6:]) for frame in response.text.split("\n\n")
                if frame.startswith("data: ")]

    return run, saved, matches


@pytest.mark.parametrize("xml", [False, True])
def test_model_invented_summary_action_fails_before_any_medication_draft(route_harness, monkeypatch, xml):
    run, saved, matches = route_harness
    monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "1" if xml else "0")
    intent = '{"action_type":"add_medication","medication":"Paracetamol","dose":"500 mg"}'
    raw = (
        f"<action_intent>{intent}</action_intent><concise_answer>Order placed.</concise_answer>"
        if xml else f"ACT|{intent}\nOrder placed."
    )
    events = run(SUMMARY_QUERY, raw)
    assert not matches, "A record-summary request must never authorize a medication lookup/draft"
    assert any(e.get("code") == "SAGE_SUMMARY_INTENT_MISMATCH" for e in events)
    assert not any(e.get("done") is True or e.get("structured_response") for e in events)
    assert not any("Order placed" in e.get("content", "") for e in events)
    assert not any("request to update the chart" in e.get("content", "") for e in events)
    # Failed model prose is not a replayable successful clinical answer.
    assert not any(args[1] == "assistant" and args[2] for args, _kwargs in saved)


@pytest.mark.parametrize("raw", [
    'ACT|{"action_type":"summarize_patient_record"}\nThe record is incomplete.',
    'ACT|{"action_type":"add_medication"',
    '<action_intent>{"action_type":"add_medication"',
])
def test_summary_action_marker_and_truncated_intent_never_become_success(route_harness, monkeypatch, raw):
    run, _saved, matches = route_harness
    monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "1" if raw.startswith("<") else "0")
    events = run(SUMMARY_QUERY, raw)
    assert any(e.get("code") == "SAGE_SUMMARY_INTENT_MISMATCH" for e in events)
    assert not any(e.get("done") is True or e.get("structured_response") for e in events)
    assert not matches


def test_valid_selector_renders_only_source_facts_and_unknowns(route_harness, monkeypatch):
    run, _saved, matches = route_harness
    monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "0")
    from services.summary_grounding import compile_sage_context, render_sage

    bundle = compile_sage_context(_facts())
    ids = tuple(f.fact_id for f in bundle.facts)
    summary = render_sage(bundle, ids)
    events = run(SUMMARY_QUERY, json.dumps({"fact_ids": ids}))
    structured = next(e["structured_response"] for e in events if "structured_response" in e)
    assert structured["concise_answer"] == summary
    metadata = next(e["metadata"] for e in events if "metadata" in e)
    assert metadata["action_state"] is None
    assert metadata["summary_grounding"]["clinical_validation"] is False
    assert any(e.get("done") is True for e in events)
    assert not matches


@pytest.mark.parametrize("raw", [
    'NOACT|No imaging has been performed.',
    'NOACT|No active medications, so there is no medication change history.',
    'NOACT|No Scribe notes, therefore no outstanding tasks.',
    '{"fact_ids":["invented-source"]}',
    '{"fact_ids":[],"answer":"The patient is stable."}',
    '{"fact_ids":',
])
def test_ungrounded_prose_never_streams_or_becomes_success(route_harness, raw):
    run, saved, matches = route_harness
    events = run(SUMMARY_QUERY, raw)
    assert any(e.get("code") == "SAGE_SUMMARY_GROUNDING_FAILED" for e in events)
    assert not any(e.get("content") or e.get("done") or e.get("structured_response") for e in events)
    assert not any(args[1] == "assistant" and args[2] for args, _kwargs in saved)
    assert not matches


def test_real_explicit_medication_order_keeps_existing_approval_flow(route_harness, monkeypatch):
    run, _saved, matches = route_harness
    monkeypatch.setenv("ORB_SAGE_XML_CONTRACT", "0")
    raw = 'ACT|{"action_type":"add_medication","medication":"Paracetamol"}\nOrder placed.'
    events = run("Add paracetamol 500mg to this patient's chart.", raw)
    metadata = next(e["metadata"] for e in events if "metadata" in e)
    assert matches == ["Paracetamol"]
    assert metadata["action_state"]["status_code"] == "request_drafted"
    assert "review and approve" in metadata["action_state"]["safe_user_message"]
    assert not any("Order placed" in e.get("content", "") for e in events)


@pytest.mark.parametrize("partial", [False, True])
def test_disconnected_summary_is_cancelled_without_error_success_or_partial_history(
    route_harness, monkeypatch, partial,
):
    from services.disconnect_stream import ClientDisconnected

    run, saved, _matches = route_harness
    provenance = []

    async def capture(**kwargs):
        provenance.append(kwargs)

    async def disconnect(source, request):
        try:
            if partial:
                yield await anext(source)
            raise ClientDisconnected("Synthetic reader disconnected")
        finally:
            await source.aclose()

    monkeypatch.setattr(sage_routes, "stream_until_disconnect", disconnect)
    monkeypatch.setattr(sage_routes, "_prov_record", capture)
    events = run(SUMMARY_QUERY, "NOACT|An unfinished synthetic summary.")
    assert not any("error" in e or e.get("done") is True for e in events)
    assert not any(args[1] == "assistant" and args[2] for args, _kwargs in saved)
    assert provenance[-1]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_external_stream_cancellation_propagates_without_false_history(route_harness, monkeypatch):
    _run, saved, _matches = route_harness
    provenance = []

    async def capture(**kwargs):
        provenance.append(kwargs)

    async def payload():
        return {"query": SUMMARY_QUERY}

    async def cancel(source, request):
        try:
            yield "NOACT|An unfinished synthetic summary."
            raise CancelledError("Synthetic request cancellation")
        finally:
            await source.aclose()

    request = SimpleNamespace(json=payload, app=FastAPI())
    monkeypatch.setattr(sage_routes, "stream_until_disconnect", cancel)
    monkeypatch.setattr(sage_routes, "_prov_record", capture)
    # The real ASGI cancellation path injects CancelledError into this iterator.
    user = {"id": "DOC001", "role": "doctor", "department": "General Medicine"}
    response = await sage_routes.sage_query(request, user)
    events = []
    with pytest.raises(CancelledError, match="Synthetic request cancellation"):
        async for event in response.body_iterator:
            events.append(event)
    assert not any('"done": true' in event or '"error"' in event for event in events)
    assert not any(args[1] == "assistant" and args[2] for args, _kwargs in saved)
    assert provenance[-1]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_grounded_summary_keeps_model_queue_and_excludes_unsourced_history(monkeypatch):
    from services.summary_grounding import compile_sage_context

    from services import llm_queue_service, thinking_mode

    bundle = compile_sage_context(_facts())
    ids = [f.fact_id for f in bundle.facts]
    ticket = object()
    calls = []

    class Provider:
        name = "offline-test"

        async def astream_chat(self, messages, options):
            assert len(messages) == 2
            assert "UNSOURCED_HISTORY" not in str(messages)
            assert "UNSOURCED_GUIDELINE" not in str(messages)
            assert all(f.fact_id in messages[1]["content"] for f in bundle.facts)
            assert options["num_ctx"] == sage_service.SCOPE_CONFIGS["patient"]["num_ctx"]
            yield json.dumps({"fact_ids": ids})

    async def choose(*args, **kwargs):
        return SimpleNamespace(model="clinical-27b", preferred="clinical-27b", downgraded=False), Provider()

    class Queue:
        async def acquire(self, **kwargs):
            assert kwargs["model"] == "clinical-27b"
            calls.append("acquired")
            return ticket

        def release(self, actual):
            assert actual is ticket
            calls.append("released")

    monkeypatch.setattr(thinking_mode, "select_provider", choose)
    monkeypatch.setattr(sage_service, "LLM_MODEL", "clinical-27b")
    monkeypatch.setattr(llm_queue_service, "llm_queue", Queue())
    output = [chunk async for chunk in sage_service.query_ollama(
        SUMMARY_QUERY, "UNSOURCED_GUIDELINE", [{"role": "assistant", "content": "UNSOURCED_HISTORY"}],
        "patient", summary_bundle=bundle,
    )]
    assert json.loads("".join(output)) == {"fact_ids": ids}
    assert calls == ["acquired", "released"]


@pytest.mark.asyncio
@pytest.mark.parametrize("downgraded", [False, True])
async def test_grounded_summary_refuses_weaker_model_substitution(monkeypatch, downgraded):
    from services.summary_grounding import GroundingError, compile_sage_context

    from services import thinking_mode

    async def choose(*args, **kwargs):
        return SimpleNamespace(model="ambient-4b", preferred="clinical-27b", downgraded=downgraded), SimpleNamespace(name="offline")

    monkeypatch.setattr(thinking_mode, "select_provider", choose)
    with pytest.raises(GroundingError, match="clinical_model_unavailable"):
        async for _ in sage_service.query_ollama(
            SUMMARY_QUERY, scope="patient", summary_bundle=compile_sage_context(_facts()),
        ):
            pytest.fail("No model content should be emitted")
