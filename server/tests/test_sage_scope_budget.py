"""Informational department requests retain the real census/prose lane."""
import json
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from auth import create_token
from database import db_session
from routes import sage_routes
from services import ai_governance, llm_providers, sage_service, thinking_mode
from services.model_router import ModelChoice


DEPARTMENT_QUERY = (
    "Give a concise overview of my department's admitted patients and "
    "identify recorded concerns for clinician review."
)


@pytest.mark.parametrize("query", [
    DEPARTMENT_QUERY,
    "Give an overview of the department's medications.",
    "Give a summary of heparin use in my department.",
    "Please give a brief summary of the patients' medication concerns.",
    "Could you give the detailed summary of the ward?",
    "Give overview of insulin and oxygen therapy.",
    "Give a quick concise overview of the medications.",
    "Don't give a summary of heparin, give an overview of the census.",
    "Give a summary of why clinicians give Zyxorin.",
    "Give an overview of whether to give Zyxorin.",
    "If we give Zyxorin, will the patient improve?",
    "The doctor reported that they give Zyxorin.",
    "Please give the patient's file to the pharmacist.",
])
def test_information_objects_are_not_administration_orders(query):
    assert sage_service.detect_mutation_intent(query) is False


@pytest.mark.parametrize("query", [
    "Give heparin 5000 units now.",
    "Give a dose of heparin.",
    "Give the patient oxygen.",
    "Please give insulin 4 units.",
    "Do not give heparin tonight.",
    "Don't give insulin.",
    "Give a summary and give heparin 5000 units.",
    "Give a concise overview; give insulin 4 units.",
    "Give an overview of the ward, then stop the heparin.",
    "Give an overview of heparin and don't give insulin tonight.",
    "Give a summary and update the chart.",
    "Please give an overview and prescribe ceftriaxone.",
    "Give a summary, and give the patient oxygen.",
    "Give a summary and give an injection of insulin.",
    "Give a concise overview and give Zyxorin to Ananya.",
    "Give a summary; give Zyxorin to Ananya.",
    "Give a summary, then give Zyxorin to Ananya.",
    "Give a summary and if glucose is high, give Zyxorin.",
    "Give a summary. Please give Zyxorin now.",
])
def test_actual_and_mixed_administration_orders_still_lock(query):
    assert sage_service.detect_mutation_intent(query) is True


def test_exact_department_query_reaches_real_broad_prompt_and_existing_budget(
    isolated_module_database, monkeypatch,
):
    """Drive retrieval, route intent selection and real provider serialization."""
    suffix = uuid.uuid4().hex[:10]
    uid, dept = f"BROAD-D-{suffix}", f"Broad Own {suffix}"
    own_names = [f"Synthetic Own Census {i} {suffix}" for i in range(5)]
    foreign_name = f"PRIVATE FOREIGN CENSUS {suffix}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, 'Budget Doctor', 'doctor', ?, 'test-only')", (uid, dept))
        for i, name in enumerate([*own_names, foreign_name]):
            pid = f"BROAD-P-{suffix}-{i}"
            patient_dept = dept if i < 5 else f"Broad Foreign {suffix}"
            db.execute("INSERT INTO patients (id, name, mrn, bed, ward, status) "
                       "VALUES (?, ?, ?, ?, ?, 'admitted')",
                       (pid, name, pid, f"TEST-{i}", patient_dept))
            db.execute("INSERT INTO admissions (patient_id, department, status, admitted_by) "
                       "VALUES (?, ?, 'active', ?)", (pid, patient_dept, uid))

    requests = []
    original_client = httpx.AsyncClient
    answer = "Recorded concerns require clinician review. The remaining census is summarized."

    def respond(request):
        requests.append(json.loads(request.content))
        frames = [
            {"message": {"content": "Recorded concerns require clinician review. "}, "done": False},
            {"message": {"content": "The remaining census is summarized."},
             "done": True, "done_reason": "stop"},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(frame) for frame in frames) + "\n")

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(respond))

    provider = llm_providers.OllamaChatProvider(model="medgemma:27b")

    async def select(*args, **kwargs):
        return ModelChoice("medgemma:27b", False, "medgemma:27b"), provider

    async def acquire(*args, **kwargs):
        return None

    from services.llm_queue_service import llm_queue
    monkeypatch.setattr(llm_providers.httpx, "AsyncClient", client)
    monkeypatch.setattr("services.model_switch.refuse_when_models_off", lambda *_: None)
    monkeypatch.setattr(thinking_mode, "select_provider", select)
    monkeypatch.setattr(llm_queue, "acquire", acquire)
    monkeypatch.setattr(llm_queue, "release", lambda: None)
    monkeypatch.setattr(ai_governance, "record_inference_soon", lambda **kw: None)
    app = FastAPI()
    app.include_router(sage_routes.router)
    with TestClient(app) as browser:
        response = browser.post(
            "/api/sage/query", json={"query": DEPARTMENT_QUERY},
            headers={"Authorization": f"Bearer {create_token(uid, 'doctor')}"},
        )

    assert response.status_code == 200
    assert len(requests) == 1
    payload = requests[0]
    assert payload["model"] == "medgemma:27b"
    assert payload["options"]["num_predict"] == 1024
    assert payload["options"]["num_ctx"] == 8192
    assert payload["options"]["temperature"] == 0.2
    assert payload["messages"][0]["content"] == sage_service.SAGE_BROAD_SYSTEM_PROMPT
    context = payload["messages"][1]["content"]
    assert all(name in context for name in own_names)
    assert foreign_name not in context
    events = [json.loads(frame.removeprefix("data: "))
              for frame in response.text.split("\n\n") if frame.startswith("data: ")]
    assert "".join(event.get("content", "") for event in events) == answer
    assert any(event.get("done") is True for event in events)
    assert not any("error" in event for event in events)
    metadata = next(event["metadata"] for event in events if "metadata" in event)
    assert metadata["query_scope"] == "BROAD"
    assert metadata["action_state"] is None
