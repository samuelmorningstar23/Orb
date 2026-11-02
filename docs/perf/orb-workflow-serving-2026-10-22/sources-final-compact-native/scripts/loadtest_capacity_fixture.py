"""Disposable capacity fixtures. No patient or runtime files are copied.

Light fixture tests the harness. --orb-worker is invoked only by the runner in
an owned temporary source copy, with the real Orb app and a fake local Ollama.
Neither fixture measures inference capacity or clinical correctness.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse


def make_manifest(nonce: str) -> dict:
    return {"synthetic_data_only": True, "server_description": "Disposable synthetic HTTP fixture, no Orb/model execution",
            "identities": [{"role": "doctor", "department": "General Medicine", "fixture_token": nonce,
                            "patient_ids": [f"SYNTH-{index:03d}" for index in range(100)]}]}


def create_app(nonce: str) -> FastAPI:
    app = FastAPI()
    app.state.counters = {"queries": 0, "unique_queries": 0, "summaries": 0, "reads": 0,
                          "peak_inflight": 0, "unauthorised": 0}
    seen = set()
    active = 0

    def authorised(value):
        if value != f"Bearer {nonce}":
            app.state.counters["unauthorised"] += 1
            raise HTTPException(401)

    @app.get("/__capacity_fixture__")
    async def identity():
        return {"disposable": True, "nonce": nonce, "model": "none"}

    @app.get("/api/vigil/patients")
    async def read(authorization: str | None = Header(default=None)):
        authorised(authorization)
        app.state.counters["reads"] += 1
        await asyncio.sleep(.003)
        return [{"synthetic": True}]

    @app.get("/api/patients/{patient_id}/summary")
    async def summary(patient_id: str, authorization: str | None = Header(default=None)):
        authorised(authorization)
        if patient_id not in make_manifest(nonce)["identities"][0]["patient_ids"]:
            raise HTTPException(404)
        app.state.counters["summaries"] += 1
        captured_at = datetime.now(timezone.utc).isoformat()
        return {"status": "ready", "patient_id": patient_id, "summary": {"overview": "Synthetic cached summary only.",
                "important_findings": [], "outstanding_items": [], "data_gaps": []},
                "generated_at": captured_at, "source_captured_at": captured_at, "source_version": 1,
                "requires_clinician_review": True}

    @app.post("/api/sage/query")
    async def query(request: Request, authorization: str | None = Header(default=None)):
        nonlocal active
        authorised(authorization)
        body = await request.json()
        patient_id = body.get("patient_id")
        if patient_id and patient_id not in make_manifest(nonce)["identities"][0]["patient_ids"]:
            raise HTTPException(404)
        app.state.counters["queries"] += 1
        seen.add(body.get("query"))
        app.state.counters["unique_queries"] = len(seen)
        active += 1
        app.state.counters["peak_inflight"] = max(active, app.state.counters["peak_inflight"])

        async def stream():
            nonlocal active
            try:
                yield 'data: {"stage":"retrieving"}\n\n'
                await asyncio.sleep(.005)
                yield 'data: {"retrieval_ms":5,"queue_ms":0}\n\n'
                for chunk in ("This synthetic response exercises ", "stream framing and concurrency only. ",
                              "It provides no evidence of clinical model performance."):
                    await asyncio.sleep(.003)
                    yield f"data: {json.dumps({'content': chunk})}\n\n"
                yield 'data: {"metadata":{"provider":{"provider":"synthetic","model":"no-model"},"prompt_tokens":2500,"output_tokens":80}}\n\n'
                yield 'data: {"done":true}\n\n'
            finally:
                active -= 1
        return StreamingResponse(stream(), media_type="text/event-stream")
    return app


def create_mock_ollama() -> FastAPI:
    app = FastAPI()
    app.state.counters = {"chat_calls": 0, "generate_calls": 0, "unique_chat_prompts": 0, "peak_inflight": 0}
    seen_chat_prompts = set()
    active = 0
    answer = ("NOACT|This is a synthetic benchmark answer, not clinical advice. The fixture reports only "
              "documented observations and does not place orders or change a chart. Review the patient's "
              "source record and escalate uncertainty to the responsible clinician. Missing medication "
              "reconciliation, absent observations, and unreviewed results should remain explicit gaps. "
              "These generated words test the real Orb retrieval, admission and sanitisation pipeline "
              "with a fake model service; they do not establish clinical correctness or inference throughput.\n"
              "<details><display_mode>concise</display_mode><clinical_assessment>Synthetic workload only.</clinical_assessment>"
              "<cited_sources>[]</cited_sources></details>")

    @app.get("/api/tags")
    async def tags():
        return {"models": [{"name": name, "model": name, "digest": "synthetic-not-real-model", "size": 1,
                            "details": {"parameter_size": "synthetic", "quantization_level": "none"}}
                           for name in ("medgemma:27b", "medgemma:4b", "gemma3:4b")]}

    @app.get("/api/version")
    async def version():
        return {"version": "synthetic-fixture"}

    @app.post("/api/show")
    async def show():
        return {"model_info": {"general.architecture": "synthetic"}, "capabilities": ["completion"]}

    @app.get("/__capacity_stats__")
    async def stats():
        return app.state.counters

    @app.post("/api/chat")
    @app.post("/api/generate")
    async def infer(request: Request):
        nonlocal active
        body = await request.json()
        chat = request.url.path.endswith("chat")
        app.state.counters["chat_calls" if chat else "generate_calls"] += 1
        model = str(body.get("model", "medgemma:27b"))
        messages = body.get("messages") or []
        text = str(body.get("prompt") or " ".join(str(message.get("content", "")) for message in messages))
        if chat:
            seen_chat_prompts.add(hashlib.sha256(text.encode()).digest())
            app.state.counters["unique_chat_prompts"] = len(seen_chat_prompts)
        is_json = body.get("format") == "json" or isinstance(body.get("format"), dict) or (
            "important_findings" in text and "outstanding_items" in text)
        content = json.dumps({"overview": "Synthetic chart summary; source records require review.",
                              "important_findings": ["Synthetic observations only."], "outstanding_items": [],
                              "data_gaps": ["Not a clinically validated model."]}) if is_json else answer
        if body.get("stream") is False:
            await asyncio.sleep(.01)
            return {"model": model, "message": {"role": "assistant", "content": content}, "response": content,
                    "done": True, "prompt_eval_count": max(1, len(text) // 4), "eval_count": len(content) // 4,
                    "prompt_eval_duration": 5_000_000, "eval_duration": 5_000_000}
        async def stream():
            nonlocal active
            active += 1
            app.state.counters["peak_inflight"] = max(active, app.state.counters["peak_inflight"])
            try:
                for offset in range(0, len(content), 80):
                    await asyncio.sleep(.003)
                    chunk = content[offset:offset + 80]
                    yield json.dumps({"model": model, "message": {"role": "assistant", "content": chunk},
                                      "response": chunk, "done": False}) + "\n"
                yield json.dumps({"model": model, "done": True, "message": {"content": ""}, "response": "",
                                  "prompt_eval_count": max(1, len(text) // 4), "eval_count": len(content) // 4,
                                  "prompt_eval_duration": 5_000_000, "eval_duration": 24_000_000}) + "\n"
            finally:
                active -= 1
        return StreamingResponse(stream(), media_type="application/x-ndjson")
    return app


def seed_disposable_hospital(directory: Path) -> list[str]:
    """Only called in the temp source copy. Never clear/update an existing DB."""
    expected = directory / "demo.db"
    if Path(os.environ.get("ORB_DB_PATH", "")).resolve() != expected.resolve() or expected.exists():
        raise RuntimeError("Refusing to seed anything except a new owned disposable database")
    from auth import hash_password
    from database import db_session, init_db
    init_db()
    password_hash = hash_password(secrets.token_urlsafe(32))
    now = datetime.now(timezone.utc).isoformat()
    patient_ids = [f"CAP-SYNTH-{index:03d}" for index in range(100)]
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,department,password_hash) VALUES(?,?,?,?,?)",
                   ("CAP-DOC", "Synthetic Capacity Clinician", "doctor", "General Medicine", password_hash))
        for index, patient_id in enumerate(patient_ids):
            clinical_notes = "\n".join(f"Synthetic recorded day {day}: observation {60 + index % 40}, task review {day % 3}; documented status stable, missing result explicit."
                                        for day in range(1, 31))
            db.execute("INSERT INTO patients(id,name,mrn,age,sex,ward,bed,attending_doctor_id,diagnosis,admission_date,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (patient_id, f"Xqz{chr(65 + index // 26)}{chr(65 + index % 26)} Vyx{chr(65 + index % 26)}", f"CAPACITY-ONLY-{index:03d}", 30 + index % 60,
                        "F" if index % 2 else "M", "General Medicine", str(index + 1), "CAP-DOC", "Synthetic evaluation record", now, "admitted"))
            db.execute("INSERT INTO admissions(id,patient_id,admitted_by,department,ward,bed,attending_doctor_id,chief_complaint,initial_diagnosis,admitting_notes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"CAP-ADM-{index:03d}", patient_id, "CAP-DOC", "General Medicine", "General Medicine", str(index + 1),
                        "CAP-DOC", "Synthetic evaluation only", "Synthetic diagnosis", clinical_notes, "active", now))
            db.execute("INSERT INTO patient_profiles(patient_id,comorbidities,allergies,current_medications,past_medical_history,updated_at) VALUES(?,?,?,?,?,?)",
                       (patient_id, json.dumps(["Synthetic hypertension"]), json.dumps(["No synthetic allergy recorded"]),
                        json.dumps([{"drug_name": "Synthetic medication", "dose": "documented dose", "frequency": "daily"}]), clinical_notes, now))
    return patient_ids


def orb_worker(directory: Path, port: int, nonce: str, *, prewarm_summaries: bool = False,
               profile: bool = False, profile_stages_only: bool = False, thread_workers: int | None = None) -> None:
    # Only this copied application's source goes ahead of installed dependencies.
    server = directory / "server"
    if Path.cwd().resolve() != server.resolve() or not directory.name.startswith("orb_capacity_"):
        raise RuntimeError("Orb worker must run inside its owned disposable source copy")
    sys.path.insert(0, str(server))
    patient_ids = seed_disposable_hospital(directory)
    full_retrieval_proof = None
    retrieval_counters = None
    if os.environ.get("ORB_CAPACITY_FULL_RETRIEVAL") == "1":
        from capacity_retrieval_fixture import prepare_full_retrieval
        full_retrieval_proof, retrieval_counters = prepare_full_retrieval(
            os.environ["ORB_EMBEDDING_MODEL"], os.environ["SAGE_RERANK_MODEL"])
    profiler = None
    if profile:
        from capacity_profiler import CapacityProfiler
        profiler = CapacityProfiler(server, collect_functions=not profile_stages_only)
        profiler.install()
    import main
    import uvicorn
    from auth import create_token
    from database import db_session
    from services.knowledge_worker import output_reuse_metrics
    # Expectations are read from this newly seeded disposable database. This
    # regression specifically measures summaries of charts with missing data;
    # a richer fixture must define a different contract instead of inheriting it.
    with db_session() as db:
        expected_rows = db.execute(
            "SELECT p.id,p.name,p.diagnosis,"
            "(SELECT COUNT(*) FROM vitals v WHERE v.patient_id=p.id) AS observations,"
            "(SELECT COUNT(*) FROM medications m WHERE m.patient_id=p.id) AS medications,"
            "(SELECT COUNT(*) FROM clinical_documents d WHERE d.patient_id=p.id AND d.doc_type='lab_result') AS labs "
            "FROM patients p WHERE p.id LIKE 'CAP-SYNTH-%'"
        ).fetchall()
    if len(expected_rows) != len(patient_ids) or any(
            r["observations"] or r["medications"] or r["labs"] for r in expected_rows):
        raise RuntimeError("Summary regression expectations require the owned empty-chart fixture")
    summary_expectations = {r["id"]: {"name": r["name"], "diagnosis": r["diagnosis"]} for r in expected_rows}
    installed_executor_workers: int | None = None
    if thread_workers is not None:
        if thread_workers not in {1, 4, 8, 16, 32}:
            raise RuntimeError("Unsupported diagnostic thread width")
        original_lifespan = main.app.router.lifespan_context
        workers: int = thread_workers
        @asynccontextmanager
        async def fixture_lifespan(app: FastAPI) -> AsyncIterator[None]:
            nonlocal installed_executor_workers
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="capacity-default")
            asyncio.get_running_loop().set_default_executor(executor)
            installed_executor_workers = executor._max_workers
            try:
                async with original_lifespan(app):
                    yield
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        main.app.router.lifespan_context = fixture_lifespan
    # This uses the production JWT verification/real permissions on every call.
    with db_session() as db:
        user = dict(db.execute("SELECT * FROM users WHERE id=?", ("CAP-DOC",)).fetchone())
    token = create_token(user["id"], user["role"])
    summary_proof = None
    if prewarm_summaries:
        from services import patient_summary_service as summary_module
        async def synthetic_summary(_payload, _job):
            return {"overview": "Synthetic preloaded draft for HTTP/cache qualification only.",
                    "important_findings": ["Synthetic chart only."], "outstanding_items": [], "data_gaps": []}
        summary_service = summary_module.PatientSummaryService(
            summary_module.SummarySettings(enabled=True, model="synthetic-summary-no-model", refresh_seconds=1800,
                                          scan_seconds=.1, max_source_chars=24000), generate=synthetic_summary)
        summary_module.patient_summary_service = summary_service
        async def preload():
            if not summary_service._lease():
                raise RuntimeError("Disposable summary service did not acquire its lease")
            for patient_id in patient_ids:
                await summary_service.get(patient_id, user)
                await summary_service.process_one()
            states = [await summary_service.get(patient_id, user) for patient_id in patient_ids]
            return {"expected_patients": len(patient_ids), "ready": sum(row["status"] == "ready" for row in states),
                    "generated": summary_service.metrics()["generated"], "generation_kind": "synthetic_generator_real_summary_service",
                    "refresh_interval_seconds": 1800, "real_30_minute_elapsed_cycle": False}
        summary_proof = asyncio.run(preload())
        if summary_proof["ready"] != len(patient_ids):
            raise RuntimeError("Not all 100 disposable summaries became ready")
    manifest = {"synthetic_data_only": True, "server_description": "Real Orb app in disposable source/DB copy; fake Ollama HTTP model service",
                "identities": [{"role": "doctor", "department": "General Medicine", "fixture_token": token,
                                "patient_ids": patient_ids}], "summary_prewarm": summary_proof,
                "full_retrieval_proof": full_retrieval_proof,
                "summary_expectations": summary_expectations}
    path = directory / "capacity-manifest.json"
    path.touch(mode=0o600)
    path.write_text(json.dumps(manifest))

    @main.app.get("/__capacity_fixture__")
    async def fixture_marker():
        from database import encryption_at_rest_status

        from services import metrics_service
        with metrics_service._LOCK:
            gauges = {name: value for (name, labels), value in metrics_service._gauges.items()
                      if not labels and name in {"orb_http_inflight", "orb_http_inflight_peak", "orb_http_inflight_limit"}}
            counters = {name: value for (name, labels), value in metrics_service._counters.items()
                        if not labels and name == "orb_http_admission_rejected_total"}
        source_paths = ("main.py", "config.py", "database.py", "request_admission.py", "services/llm_queue_service.py",
                        "services/local_inference_pool.py", "services/ollama_gateway.py", "services/patient_summary_service.py",
                        "services/local_inference_routing.py", "services/llm_providers.py", "services/thinking_mode.py",
                        "services/local_model_http.py",
                        "services/disconnect_stream.py",
                        "services/model_router.py",
                        "services/ai_governance.py",
                        "services/sage_retrieval_executor.py",
                        "services/clinical_knowledge.py", "services/knowledge_worker.py", "services/local_transformers.py",
                        "services/sage_service.py", "services/gpu_coordinator.py", "routes/sage_routes.py",
                        "routes/patient_summary_routes.py", "migrations_his/m0089_patient_summary_snapshots.py",
                        "migrations_his/m0090_sage_retrieval_indexes.py",
                        "auth.py", "permissions.py", "db_backend.py")
        return {"disposable": True, "nonce": nonce, "model": "operator-origin", "real_orb_routes": True,
                "full_retrieval_proof": full_retrieval_proof,
                "knowledge_worker_output_reuse": output_reuse_metrics(),
                "retrieval_runtime": retrieval_counters.report() if retrieval_counters is not None else None,
                "retrieval_executor": (main.app.state.sage_retrieval_executor.metrics()
                                       if getattr(main.app.state, "sage_retrieval_executor", None) is not None else None),
                "diagnostic_default_executor_workers": thread_workers,
                "effective_default_executor_workers": installed_executor_workers or getattr(
                    getattr(asyncio.get_running_loop(), "_default_executor", None), "_max_workers", None),
                "summary_prewarm": summary_proof, "http_admission": {**gauges, **counters},
                "database_encryption": encryption_at_rest_status(),
                "copied_source_sha256": {str(path): hashlib.sha256((server / path).read_bytes()).hexdigest()
                                         for path in source_paths if (server / path).is_file()}}
    # The SPA fallback is mounted last in main; the test-only marker needs to
    # precede it. This changes only the throwaway process, never app source.
    main.app.router.routes.insert(0, main.app.router.routes.pop())
    if profiler is not None:
        active_profiler = profiler
        @main.app.post("/__capacity_profile__/{action}")
        async def profile_control(action: str, x_orb_capacity_nonce: str | None = Header(default=None)) -> dict[str, Any]:
            if x_orb_capacity_nonce != nonce:
                raise HTTPException(403)
            if action == "start":
                active_profiler.start()
                return {"diagnostic_only": True, "active": True}
            if action == "stop":
                return active_profiler.stop()
            raise HTTPException(404)
        main.app.router.routes.insert(0, main.app.router.routes.pop())
    uvicorn.run(main.socket_app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--orb-worker", action="store_true")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--prewarm-summaries", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--profile-stages-only", action="store_true")
    parser.add_argument("--thread-workers", type=int, choices=(1, 4, 8, 16, 32))
    args = parser.parse_args()
    if not args.orb_worker:
        parser.error("Only the owned Orb worker mode is a standalone entry point")
    try:
        orb_worker(args.directory, args.port, args.nonce, prewarm_summaries=args.prewarm_summaries, profile=args.profile,
                   profile_stages_only=args.profile_stages_only, thread_workers=args.thread_workers)
    except BaseException as error:
        status = args.directory / "capacity-startup-stage.json"
        if (args.directory.name.startswith("orb_capacity_") and Path.cwd().resolve() == (args.directory / "server").resolve()
                and status.is_file() and status.stat().st_size <= 4096):
            data = json.loads(status.read_text())
            data["exception_type"] = type(error).__name__
            status.write_text(json.dumps(data))
        raise
