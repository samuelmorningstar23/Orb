"""Synthetic correctness/race/permission tests; no model downloads or clinical data.

The injected generator is deliberately not evidence of inference capacity or
clinical accuracy. Real schema/triggers/lease/cache/jobs/HTTP auth are exercised.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from auth import get_current_user
from database import db_session, run_db
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routes import patient_summary_routes
from services.patient_summary_service import (
    SOURCE_FIELDS,
    PatientSummaryService,
    SummaryError,
    SummarySettings,
    validate_summary,
)

from services import facility_service

pytestmark = pytest.mark.usefixtures("isolated_module_database")


@pytest.fixture(autouse=True)
def clear_summary_work(isolated_module_database):
    # Each test owns scheduling state; retained invented clinical records are
    # still useful for demonstrating that the real census is not a bed count.
    with db_session() as db:
        db.execute("DELETE FROM patient_summary_scheduler_lease")
        db.execute("DELETE FROM patient_summary_jobs")
        db.execute("DELETE FROM patient_summary_snapshots")


def sample_summary(text="Synthetic record draft, requiring review"):
    return {
        "overview": text,
        "important_findings": ["A recorded synthetic finding"],
        "outstanding_items": [],
        "data_gaps": ["No imaging report supplied"],
    }


@pytest.fixture
def world():
    tag = uuid.uuid4().hex
    pid, aid, uid = "SUM-P-" + tag, "SUM-A-" + tag, "SUM-U-" + tag
    with db_session() as db:
        db.execute(
            "INSERT INTO users(id, name, role, department, password_hash) VALUES (?, 'Summary Doctor', 'doctor', 'Summary Ward', 'x')",
            (uid,),
        )
        db.execute(
            "INSERT INTO patients(id, name, mrn, diagnosis, status, registered_facility_id) "
            "VALUES (?, 'Invented Summary Patient', ?, 'Invented record condition', 'admitted', 1)",
            (pid, "SUM-MRN-" + tag),
        )
        db.execute(
            "INSERT INTO admissions(id, patient_id, admitted_by, department, status, facility_id, created_at) "
            "VALUES (?, ?, ?, 'Summary Ward', 'active', 1, '2026-10-21T00:00:00+00:00')",
            (aid, pid, uid),
        )
        db.execute(
            "INSERT INTO vitals(patient_id, timestamp, heart_rate) VALUES (?, '2026-10-21T01:00:00+00:00', 81)",
            (pid,),
        )
    facility_service.invalidate_cache()
    return {
        "patient": pid,
        "admission": aid,
        "user": {
            "id": uid,
            "name": "Summary Doctor",
            "role": "doctor",
            "department": "Summary Ward",
        },
    }


@pytest.fixture
def clock():
    return [1792548000.0]


@pytest.fixture
def service(clock):
    async def generator(payload, job):
        assert payload["patients"][0]["id"] == job["patient_id"]
        return sample_summary()

    return PatientSummaryService(
        SummarySettings(
            enabled=True, model="synthetic-test-model", max_source_chars=50000
        ),
        clock=lambda: clock[0],
        generate=generator,
    )


async def ready(service, world):
    assert await run_db(service._lease)
    answer = await service.get(world["patient"], world["user"])
    assert answer["summary"] is None
    assert await service.process_one()
    answer = await service.get(world["patient"], world["user"])
    assert answer["status"] == "ready", answer
    return answer


@pytest.mark.asyncio
async def test_snapshot_survives_service_restart_and_never_generates_on_read(
    service, world, clock
):
    answer = await ready(service, world)
    assert answer["generated_at"] and answer["requires_clinician_review"] is True

    async def forbidden(*args):
        raise AssertionError("Read must never call an LLM")

    restarted = PatientSummaryService(
        service.settings, clock=lambda: clock[0], generate=forbidden
    )
    result = await restarted.get(world["patient"], world["user"])
    assert result["status"] == "ready"
    assert result["summary"] == answer["summary"]


@pytest.mark.asyncio
async def test_200_missing_reads_coalesce_to_one_durable_job(service, world):
    answers = await asyncio.gather(
        *(service.get(world["patient"], world["user"]) for _ in range(200))
    )
    assert all(
        result["status"] == "unavailable" and result["summary"] is None
        for result in answers
    )
    with db_session() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM patient_summary_jobs WHERE patient_id = ?",
                (world["patient"],),
            ).fetchone()[0]
            == 1
        )
    assert service.metrics()["generated"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["update", "delete", "allergy", "critical"])
async def test_changed_or_corrected_record_is_never_served_as_current(
    service, world, change
):
    old = await ready(service, world)
    with db_session() as db:
        if change == "update":
            db.execute(
                "UPDATE vitals SET heart_rate = 131 WHERE patient_id = ?",
                (world["patient"],),
            )
        elif change == "delete":
            db.execute("DELETE FROM vitals WHERE patient_id = ?", (world["patient"],))
        elif change == "allergy":
            db.execute(
                "INSERT INTO patient_allergies(patient_id, substance, reaction, severity) VALUES (?, 'invented-allergen', 'synthetic reaction', 'severe')",
                (world["patient"],),
            )
        else:
            db.execute(
                "INSERT INTO risk_scores(patient_id, score, level) VALUES (?, 9, 'critical')",
                (world["patient"],),
            )
    response = await service.get(world["patient"], world["user"])
    assert response["reason"] == "source_changed"
    assert response["summary"] is None
    assert response["source_version"] > old["source_version"]


@pytest.mark.asyncio
async def test_continuous_observations_do_not_starve_publication_or_create_job_storm(
    service, world
):
    async def streaming_observations(payload, job):
        with db_session() as db:
            for index in range(50):
                db.execute(
                    "INSERT INTO vitals(patient_id, timestamp, heart_rate) VALUES (?, ?, 82)",
                    (world["patient"], f"2026-10-21T02:00:{index:02d}+00:00"),
                )
        return sample_summary("An explicitly as-of synthetic draft")

    service._generate = streaming_observations
    assert await run_db(service._lease)
    await service.get(world["patient"], world["user"])
    assert await service.process_one()
    results = await asyncio.gather(
        *(service.get(world["patient"], world["user"]) for _ in range(200))
    )
    assert all(
        result["status"] == "outdated" and result["reason"] == "new_observations"
        for result in results
    )
    assert all(
        result["summary"] and result["as_of_draft"] and result["source_changed"]
        for result in results
    )
    assert service.metrics()["generated"] == 1
    assert service.metrics()["discarded_changed"] == 0
    await run_db(service._schedule)
    with db_session() as db:
        row = db.execute(
            "SELECT state, next_run_at FROM patient_summary_jobs WHERE patient_id = ?",
            (world["patient"],),
        ).fetchone()
    assert (
        row["state"] == "ready"
    )  # periodic refresh, not one generation per monitored observation


def test_bounded_recent_streams_disclose_omissions_and_preserve_source_ids(
    service, world
):
    with db_session() as db:
        for index in range(40):
            db.execute(
                "INSERT INTO vitals(patient_id, timestamp, heart_rate) VALUES (?, ?, 83)",
                (world["patient"], f"2026-10-21T02:01:{index:02d}+00:00"),
            )
    assert service._lease()
    service._read(world["patient"])
    job = service._claim()
    payload, metadata = service._capture(job)
    assert len(payload["vitals"]) == 24
    coverage = metadata["section_coverage"]["vitals"]
    assert coverage["matching_rows"] == 41
    assert coverage["omitted_older_rows"] == 17
    assert metadata["partial_coverage"] is True
    assert metadata["is_complete_patient_history"] is False
    assert payload["_source_coverage"]["partial_coverage"] is True
    assert all(
        row["id"] for row in metadata["source_references"] if row["table"] == "vitals"
    )


def test_recent_stream_order_uses_instants_not_mixed_timestamp_spelling(service, world):
    with db_session() as db:
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (world["patient"],))
        for index in range(24):
            # The 'T' spelling sorts above a later SQLite-space timestamp if
            # ORDER BY accidentally uses raw text instead of an instant.
            db.execute(
                "INSERT INTO vitals(patient_id, timestamp, heart_rate) VALUES (?, ?, 80)",
                (world["patient"], f"2026-10-21T02:00:{index:02d}+00:00"),
            )
        db.execute(
            "INSERT INTO vitals(patient_id, timestamp, heart_rate) VALUES (?, '2026-10-21 18:00:00', 99)",
            (world["patient"],),
        )
        # This looks later as text, but is 01:00 UTC and must be excluded.
        db.execute(
            "INSERT INTO vitals(patient_id, timestamp, heart_rate) VALUES (?, '2026-10-21T06:30:00+05:30', 11)",
            (world["patient"],),
        )
    assert service._lease()
    service._read(world["patient"])
    payload, metadata = service._capture(service._claim())
    assert payload["vitals"][0]["heart_rate"] == 99
    assert not any(row["heart_rate"] == 11 for row in payload["vitals"])
    assert (
        metadata["section_coverage"]["vitals"]["newest_included_at"]
        == "2026-10-21 18:00:00"
    )


@pytest.mark.asyncio
async def test_source_change_while_model_runs_discards_result(service, world, clock):
    async def racing(payload, job):
        with db_session() as db:
            db.execute(
                "UPDATE patients SET diagnosis = 'A newer invented condition' WHERE id = ?",
                (world["patient"],),
            )
        return sample_summary("Draft referring to the OLD condition")

    service._generate = racing
    assert await run_db(service._lease)
    await service.get(world["patient"], world["user"])
    assert await service.process_one()
    with db_session() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM patient_summary_snapshots WHERE patient_id = ?",
                (world["patient"],),
            ).fetchone()[0]
            == 0
        )
    assert service.metrics()["discarded_changed"] == 1
    assert (await service.get(world["patient"], world["user"]))["summary"] is None


@pytest.mark.asyncio
async def test_changed_admission_and_facility_cannot_reuse_previous_draft(
    service, world
):
    await ready(service, world)
    with db_session() as db:
        db.execute(
            "UPDATE admissions SET status = 'discharged' WHERE id = ?",
            (world["admission"],),
        )
        db.execute(
            "INSERT INTO admissions(id, patient_id, admitted_by, department, status, facility_id, created_at) "
            "VALUES (?, ?, ?, 'Other Ward', 'active', 991, '2026-10-22T00:00:00+00:00')",
            (world["admission"] + "-next", world["patient"], world["user"]["id"]),
        )
    # Explicit real scope parameters exercise the second service-layer gate.
    with pytest.raises(HTTPException) as err:
        await run_db(service._read, world["patient"], "Summary Ward", frozenset({1}))
    assert err.value.status_code == 404
    response = await run_db(
        service._read, world["patient"], "Other Ward", frozenset({991})
    )
    assert response["status"] == "unavailable"
    assert response["summary"] is None
    assert response["facility_id"] == 991


@pytest.mark.asyncio
async def test_cache_expires_at_30_minutes_not_after_it(service, world, clock):
    await ready(service, world)
    clock[0] += 1799
    assert (await service.get(world["patient"], world["user"]))["status"] == "ready"
    clock[0] += 1
    answer = await service.get(world["patient"], world["user"])
    assert answer["reason"] == "expired" and answer["summary"] is None


@pytest.mark.asyncio
async def test_generation_time_does_not_extend_source_freshness_budget(
    service, world, clock
):
    async def slow(*args):
        clock[0] += 120
        # The scheduler renews the lease while the model runs in production.
        assert service._lease()
        return sample_summary()

    service._generate = slow
    await ready(service, world)
    answer = await service.get(world["patient"], world["user"])
    assert answer["source_age_seconds"] == 120
    assert answer["refresh_after_seconds"] == 1680
    clock[0] += 1680
    assert (await service.get(world["patient"], world["user"]))["summary"] is None


@pytest.mark.asyncio
async def test_other_patient_and_model_never_get_cached_prose(service, world, clock):
    await ready(service, world)
    other = PatientSummaryService(
        replace(service.settings, model="different-model"), clock=lambda: clock[0]
    )
    assert (await other.get(world["patient"], world["user"]))["summary"] is None
    revised = PatientSummaryService(
        replace(service.settings, model_revision="a-new-immutable-artifact"),
        clock=lambda: clock[0],
    )
    assert (await revised.get(world["patient"], world["user"]))["summary"] is None
    with pytest.raises(HTTPException):
        await service.get("no-such-synthetic-patient", world["user"])


@pytest.mark.asyncio
async def test_generation_failure_keeps_explicit_unavailable(service, world):
    async def invalid(*args):
        return {"overview": "A partial invalid response"}

    service._generate = invalid
    assert await run_db(service._lease)
    await service.get(world["patient"], world["user"])
    await service.process_one()
    response = await service.get(world["patient"], world["user"])
    assert response["summary"] is None
    assert response["job"]["state"] == "failed"
    assert response["job"]["error_code"] == "invalid_model_output"


@pytest.mark.asyncio
async def test_lease_elects_only_one_scheduler_and_fences_old_publisher(
    service, world, clock
):
    assert await run_db(service._lease)
    await service.get(world["patient"], world["user"])
    job = await run_db(service._claim)
    _payload, metadata = await run_db(service._capture, job)
    second = PatientSummaryService(service.settings, clock=lambda: clock[0])
    assert await run_db(second._lease) is False
    clock[0] += 31
    assert await run_db(second._lease) is True
    assert (
        await run_db(service._publish, job, sample_summary(), metadata, clock[0])
        is False
    )
    await run_db(second._schedule)
    assert await run_db(second._claim) is not None


@pytest.mark.asyncio
async def test_job_admission_is_bounded(service, world):
    service.settings = replace(service.settings, max_pending=1)
    with db_session() as db:
        db.execute("UPDATE patient_summary_jobs SET state = 'inactive'")
    first = await service.get(world["patient"], world["user"])
    assert first["job"]["state"] == "queued"
    with db_session() as db:
        p2 = world["patient"] + "-2"
        db.execute(
            "INSERT INTO patients(id, name, mrn, status) VALUES (?, 'Second Invented Person', ?, 'admitted')",
            (p2, p2),
        )
        db.execute(
            "INSERT INTO admissions(id, patient_id, admitted_by, department, status, facility_id) VALUES (?, ?, ?, 'Summary Ward', 'active', 1)",
            (p2, p2, world["user"]["id"]),
        )
    second = await service.get(p2, world["user"])
    assert second["job"] == {"state": "deferred", "error_code": "queue_full"}


def test_real_census_is_staggered_and_excludes_discharged(service, world, clock):
    with db_session() as db:
        db.execute("UPDATE patient_summary_jobs SET state = 'inactive'")
        db.execute(
            "UPDATE admissions SET status = 'discharged' WHERE id != ? AND status = 'active'",
            (world["admission"],),
        )
        for index in range(5):
            pid = world["patient"] + str(index)
            db.execute(
                "INSERT INTO patients(id, name, mrn, status) VALUES (?, 'Staggered Invented Person', ?, 'admitted')",
                (pid, pid),
            )
            db.execute(
                "INSERT INTO admissions(id, patient_id, admitted_by, department, status, facility_id) VALUES (?, ?, ?, 'Summary Ward', ?, 1)",
                (
                    pid,
                    pid,
                    world["user"]["id"],
                    "discharged" if index == 4 else "active",
                ),
            )
    assert service._lease()
    assert service._schedule() == 5
    with db_session() as db:
        rows = db.execute(
            "SELECT next_run_at FROM patient_summary_jobs WHERE state = 'queued'"
        ).fetchall()
    dues = sorted(row[0] for row in rows)
    assert len(set(dues)) == 5
    assert dues[0] == clock[0]
    assert dues[-1] < clock[0] + 1800


@pytest.mark.asyncio
async def test_bounded_workers_cancel_cleanly_without_background_model_leak(
    service, world
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service._generate = blocked
    service.settings = replace(service.settings, scan_seconds=0.05)
    await service.get(world["patient"], world["user"])
    await service.start()
    await asyncio.wait_for(started.wait(), timeout=3)
    assert service.metrics()["workers"] == 1
    await service.stop()
    assert cancelled.is_set()
    assert service.metrics()["workers"] == 0
    with db_session() as db:
        assert (
            db.execute(
                "SELECT 1 FROM patient_summary_scheduler_lease WHERE owner = ?",
                (service.owner,),
            ).fetchone()
            is None
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"overview":"cut off',
        '{"overview":"a"}',
        "```json\n{}\n```",
        json.dumps({**sample_summary(), "extra": "not permitted"}),
    ],
)
def test_incomplete_or_unexpected_model_output_is_rejected(raw):
    with pytest.raises(SummaryError):
        validate_summary(raw)


def test_source_contract_has_transactional_revision_triggers_for_every_read_source():
    with db_session() as db:
        for table in (*SOURCE_FIELDS, "observations"):
            for event in ("insert", "update", "delete"):
                assert db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name = ?",
                    (f"summary_revision_{table}_{event}",),
                ).fetchone(), table


def test_interfaced_labs_use_canonical_live_final_current_admission_reader(
    service, world
):
    from services.lab_observations import summary_observations

    with db_session() as db:
        for status, observed, link, value in (
            ("final", "2026-10-21 03:00:00", None, "1.20"),
            ("corrected", "2026-10-21T09:00:00+05:30", None, "1.30"),
            ("preliminary", "2026-10-21T05:00:00+00:00", None, "99"),
            ("entered-in-error", "2026-10-21T05:00:00+00:00", None, "99"),
            ("final", "2026-10-20T23:00:00+00:00", None, "99"),
            ("final", "2026-10-21T05:00:00+00:00", 999999, "99"),
        ):
            db.execute(
                "INSERT INTO observations(patient_id, observation_name, observation_code, status, observed_at, lab_result_id, value_text, unit, source) "
                "VALUES (?, 'Invented test', 'SYNTHETIC-TEST', ?, ?, ?, ?, 'synthetic-unit', 'synthetic_test')",
                (world["patient"], status, observed, link, value),
            )
        total, rows = summary_observations(
            db, world["patient"], since="2026-10-21T00:00:00+00:00"
        )
        assert total == 2
        assert [row["value_text"] for row in rows] == ["1.30", "1.20"]
    assert service._lease()
    service._read(world["patient"])
    payload, metadata = service._capture(service._claim())
    assert [row["value_text"] for row in payload["interfaced_lab_results"]] == [
        "1.30",
        "1.20",
    ]
    assert metadata["row_counts"]["interfaced_lab_results"] == 2


def test_allergy_review_and_held_medication_state_are_preserved(service, world):
    with db_session() as db:
        db.execute(
            "INSERT INTO patient_allergy_assessments(patient_id, status, reason, source, assessed_by, assessed_at) "
            "VALUES (?, 'unable_to_obtain', 'Synthetic missing history', 'opd', ?, '2026-10-21T03:00:00+00:00')",
            (world["patient"], world["user"]["id"]),
        )
        medid = "SUM-MED-" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO med_orders(id, patient_id, admission_id, status, held_from, source, needs_verification, drug_name, dose, entered_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'held', 'prescribed', 'llm_extracted', 1, 'Invented Test Tablet', '5 mg', ?, '2026-10-21T01:00:00+00:00', '2026-10-21T03:00:00+00:00')",
            (medid, world["patient"], world["admission"], world["user"]["id"]),
        )
        db.execute(
            "INSERT INTO medications(patient_id, drug_name, dosage, route, frequency, status, prescribed_by, source, held_at, hold_reason, med_order_id) "
            "VALUES (?, 'Invented Test Tablet', '5 mg', 'Oral', 'OD', 'active', ?, 'llm_extracted', '2026-10-21T03:00:00+00:00', 'Synthetic hold', ?)",
            (world["patient"], world["user"]["id"], medid),
        )
    assert service._lease()
    service._read(world["patient"])
    payload, _ = service._capture(service._claim())
    assert payload["patient_allergy_assessments"][0]["status"] == "unable_to_obtain"
    assert (
        payload["patient_allergy_assessments"][0]["reason"]
        == "Synthetic missing history"
    )
    assert payload["med_orders"][0]["status"] == "held"
    assert payload["med_orders"][0]["needs_verification"] == 1
    assert payload["medications"][0]["hold_reason"] == "Synthetic hold"
    assert payload["medications"][0]["source"] == "llm_extracted"


def test_source_budget_fails_honestly_instead_of_head_trimming(service, world):
    service._lease()
    service._read(world["patient"])
    job = service._claim()
    service.settings = replace(service.settings, max_source_chars=20)
    with pytest.raises(SummaryError, match="source_context_too_large"):
        service._capture(job)


def test_actual_route_requires_permissions_and_department_scope(
    service, world, monkeypatch
):
    app = FastAPI()
    app.include_router(patient_summary_routes.router)
    monkeypatch.setattr(patient_summary_routes.svc, "patient_summary_service", service)
    app.dependency_overrides[get_current_user] = lambda: world["user"]
    with TestClient(app) as client:
        response = client.get(f"/api/patients/{world['patient']}/summary")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["summary"] is None
        app.dependency_overrides[get_current_user] = lambda: {
            **world["user"],
            "department": "Unrelated Ward",
        }
        assert (
            client.get(f"/api/patients/{world['patient']}/summary").status_code == 404
        )
        app.dependency_overrides[get_current_user] = lambda: {
            **world["user"],
            "role": "patient",
        }
        assert (
            client.post(f"/api/patients/{world['patient']}/summary/refresh").status_code
            == 403
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["complete", "length", "incomplete", "invalid_json", "transport_error"]
)
async def test_local_provider_completion_validation_and_exact_ticket_cleanup(
    service, world, monkeypatch, outcome
):
    from services.summary_grounding import compile_snapshot, render_cached

    from services import llm_queue_service, ollama_gateway

    source = {"patients": [{"id": world["patient"], "diagnosis": "Invented recorded condition"}]}
    bundle = compile_snapshot(source, {"patient_id": world["patient"]})
    selection = tuple(fact.fact_id for fact in bundle.facts)

    ticket = object()
    calls = []

    class Queue:
        async def acquire(self, **kwargs):
            assert kwargs["model"] == service.settings.model
            calls.append("acquired")
            return ticket

        def release(self, actual):
            assert actual is ticket
            calls.append("released")

    async def post(client, path, payload, **kwargs):
        assert path == "/api/chat" and payload["format"]["type"] == "object"
        assert set(payload["format"]["properties"]) == {"fact_ids"}
        assert payload["model"] == service.settings.model
        assert kwargs["patient_id"] == world["patient"]
        assert (
            kwargs["purpose"] == "patient_summary_snapshot"
            and kwargs["yield_to_clinician"]
        )
        if outcome == "transport_error":
            raise OSError("Synthetic transport failure")
        data = {
            "done": outcome != "incomplete",
            "done_reason": "length" if outcome == "length" else "stop",
            "message": {
                "content": '{"fact_ids":'
                if outcome == "invalid_json"
                else json.dumps({"fact_ids": list(selection)})
            },
        }
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    monkeypatch.setattr(llm_queue_service, "llm_queue", Queue())
    monkeypatch.setattr(ollama_gateway, "post", post)
    if outcome == "complete":
        result = await service._generate_local(
            source, {"patient_id": world["patient"]}
        )
        assert result == render_cached(bundle, selection)
    else:
        with pytest.raises((SummaryError, OSError)):
            await service._generate_local(
                source, {"patient_id": world["patient"]}
            )
    assert calls == ["acquired", "released"]


@pytest.mark.asyncio
async def test_reserved_summary_worker_runs_during_continuous_interactive_admission(service, world, monkeypatch):
    from services.local_inference_pool import LocalInferencePool, WorkerSpec
    from services.summary_grounding import compile_snapshot, render_cached

    from services import llm_queue_service, ollama_gateway

    source = {"patients": [{"id": world["patient"], "diagnosis": "Invented recorded condition"}]}
    bundle = compile_snapshot(source, {"patient_id": world["patient"]})
    selection = tuple(fact.fact_id for fact in bundle.facts)

    model = service.settings.model
    pool = LocalInferencePool([
        WorkerSpec("foreground", "http://127.0.0.1:11435", (model,), qualification="synthetic test"),
        WorkerSpec("summaries", "http://127.0.0.1:11436", (model,), lanes=("background",), qualification="synthetic test"),
    ])
    queue = llm_queue_service.PriorityLLMQueue(pool=pool)
    queue.governor.enabled = True
    monkeypatch.setattr(llm_queue_service, "llm_queue", queue)
    held, release = asyncio.Event(), asyncio.Event()

    async def foreground():
        ticket = await queue.acquire(priority=1, model=model)
        try:
            held.set()
            await release.wait()
        finally:
            queue.release(ticket)

    async def post(client, path, payload, **kwargs):
        assert queue.preempt_requested
        assert queue.current_worker().worker.name == "summaries"
        assert kwargs["yield_to_clinician"] is False
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "done": True, "done_reason": "stop", "message": {"content": json.dumps({"fact_ids": list(selection)})},
        })

    monkeypatch.setattr(ollama_gateway, "post", post)
    ongoing = asyncio.create_task(foreground())
    await held.wait()
    try:
        async with asyncio.timeout(0.5):
            assert await service._generate_local(source, {"patient_id": world["patient"]}) == render_cached(bundle, selection)
    finally:
        release.set()
        await ongoing
    assert pool.active == pool.waiting == 0


@pytest.mark.asyncio
async def test_paused_models_do_not_accumulate_unserviceable_refresh_jobs(
    service, world, monkeypatch
):
    from services import model_switch

    service._custom_generate = False
    monkeypatch.setattr(model_switch, "background_models_enabled", lambda: False)
    result = await service.get(world["patient"], world["user"])
    assert result["reason"] == "generation_paused"
    assert result["job"]["state"] == "paused"
    assert result["summary"] is None
    with db_session() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM patient_summary_jobs WHERE patient_id = ?",
                (world["patient"],),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_erasure_does_not_recreate_derived_patient_identifiers(service, world):
    await ready(service, world)
    with db_session() as db:
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (world["patient"],))
        db.execute("DELETE FROM admissions WHERE patient_id = ?", (world["patient"],))
        db.execute("DELETE FROM patients WHERE id = ?", (world["patient"],))
        for table in (
            "patient_summary_snapshots",
            "patient_summary_jobs",
            "patient_summary_source_versions",
        ):
            assert (
                db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?",
                    (world["patient"],),
                ).fetchone()[0]
                == 0
            )
        # A delayed write to a non-FK clinical ledger must not resurrect the
        # erased identifier in the summary's derived revision/cache tables.
        db.execute(
            "INSERT INTO med_orders(id, patient_id, status, source, drug_name, entered_by, created_at, updated_at) "
            "VALUES (?, ?, 'draft', 'synthetic_test', 'Invented Tablet', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("SUM-ORPHAN-" + uuid.uuid4().hex, world["patient"], world["user"]["id"]),
        )
        assert (
            db.execute(
                "SELECT 1 FROM patient_summary_source_versions WHERE patient_id = ?",
                (world["patient"],),
            ).fetchone()
            is None
        )
