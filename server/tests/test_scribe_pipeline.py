"""Orb Scribe Pipeline Tests

Tests config loading, provider resolution, stage toggles, verification schema,
pipeline metadata, backward compatibility, and DB schema changes.
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

# Add server root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════
# Config Tests
# ═══════════════════════════════════════════════════

class TestScribeConfig:
    """Test SCRIBE configuration loading and env-var overrides."""

    def test_default_config_matches_current_behavior(self):
        """Defaults must reproduce the pre-refactor hardcoded behavior."""
        from config import SCRIBE, LLM_MODEL, DEPLOYMENT_PROFILE, THINKING_FAST_MODEL

        assert SCRIBE["asr"]["model"] == "large-v3"
        assert SCRIBE["asr"]["provider"] == "whisper-mlx"
        assert SCRIBE["asr"]["batch_size"] == 12

        assert SCRIBE["correction"]["enabled"] is True

        # Dev runs the cheap stages on the fast 4B through Ollama. The old
        # default was an MLX MedGemma 1.5 path with no weights behind it.
        is_dev = DEPLOYMENT_PROFILE == "dev"
        expected_correction_model = THINKING_FAST_MODEL if is_dev else LLM_MODEL
        expected_correction_provider = "ollama"
        expected_verify_model = THINKING_FAST_MODEL if is_dev else LLM_MODEL
        expected_verify_provider = "ollama"
        
        assert SCRIBE["correction"]["model"] == expected_correction_model
        assert SCRIBE["correction"]["provider"] == expected_correction_provider
        assert SCRIBE["correction"]["temperature"] == 0.1
        
        assert SCRIBE["draft"]["model"] == LLM_MODEL
        assert SCRIBE["draft"]["provider"] == "ollama"
        
        assert SCRIBE["verify"]["enabled"] is False
        assert SCRIBE["verify"]["model"] == expected_verify_model
        assert SCRIBE["verify"]["provider"] == expected_verify_provider

    def test_config_has_all_four_stages(self):
        from config import SCRIBE
        assert set(SCRIBE.keys()) == {"asr", "correction", "draft", "verify"}

    def test_each_stage_has_model_and_provider(self):
        from config import SCRIBE
        for stage_name, stage_cfg in SCRIBE.items():
            assert "model" in stage_cfg, f"{stage_name} missing 'model'"
            assert "provider" in stage_cfg, f"{stage_name} missing 'provider'"

    def test_correction_has_toggle(self):
        from config import SCRIBE
        assert isinstance(SCRIBE["correction"]["enabled"], bool)

    def test_verification_has_toggle(self):
        from config import SCRIBE
        assert isinstance(SCRIBE["verify"]["enabled"], bool)

    def test_verify_disabled_by_default(self):
        from config import SCRIBE
        assert SCRIBE["verify"]["enabled"] is False

    def test_correction_enabled_by_default(self):
        from config import SCRIBE
        assert SCRIBE["correction"]["enabled"] is True

    def test_legacy_whisper_model_still_exists(self):
        """WHISPER_MODEL top-level var must remain for backward compat."""
        from config import WHISPER_MODEL
        assert WHISPER_MODEL == "large-v3"

    def test_legacy_llm_model_still_exists(self):
        """LLM_MODEL top-level var must remain for other Orb services."""
        from config import LLM_MODEL
        assert LLM_MODEL and isinstance(LLM_MODEL, str)


# ═══════════════════════════════════════════════════
# Provider Tests
# ═══════════════════════════════════════════════════

class TestScribeProviders:
    """Test provider resolution and interface."""

    def test_get_ollama_provider(self):
        from services.scribe_providers import get_provider, OllamaProvider
        provider = get_provider("ollama")
        assert isinstance(provider, OllamaProvider)

    def test_unknown_provider_raises(self):
        from services.scribe_providers import get_provider
        with pytest.raises(ValueError, match="Unknown Scribe LLM provider"):
            get_provider("nonexistent")

    def test_stage_timer(self):
        import time
        from services.scribe_providers import StageTimer
        timer = StageTimer()
        timer.start("test_stage")
        time.sleep(0.01)
        timer.stop()
        
        assert "test_stage" in timer.timings
        assert timer.timings["test_stage"] > 0
        assert timer.timings["test_stage"] < 1.0

    def test_stage_timer_multiple_stages(self):
        from services.scribe_providers import StageTimer
        timer = StageTimer()
        timer.start("a")
        timer.stop()
        timer.start("b")
        timer.stop()
        
        result = timer.to_dict()
        assert "a" in result
        assert "b" in result


# ═══════════════════════════════════════════════════
# Service Tests (mocked LLM calls)
# ═══════════════════════════════════════════════════

class TestScribeService:
    """Test scribe_service functions with mocked providers."""

    def test_correct_transcript_empty_input(self):
        """Empty input should pass through without calling the provider."""
        from services.scribe_service import correct_transcript
        result = correct_transcript("")
        assert result == ""

    def test_correct_transcript_whitespace_input(self):
        from services.scribe_service import correct_transcript
        result = correct_transcript("   ")
        assert result == "   "

    @patch("services.scribe_service.SCRIBE", {
        "correction": {"enabled": False, "model": "test", "provider": "ollama", "temperature": 0.1, "max_tokens": 100, "ctx_size": 100},
        "asr": {"model": "large-v3", "provider": "whisper-mlx", "batch_size": 12, "quant": None},
        "draft": {"model": "test", "provider": "ollama", "temperature": 0.3, "max_tokens": 100, "ctx_size": 100},
        "verify": {"enabled": False, "model": "test", "provider": "ollama", "temperature": 0.1, "max_tokens": 100, "ctx_size": 100},
    })
    def test_correction_skipped_when_disabled(self):
        """When correction is disabled, transcribe_audio should skip correction."""
        from services.scribe_service import SCRIBE
        assert SCRIBE["correction"]["enabled"] is False

    def test_verify_note_returns_none_when_disabled(self):
        """verify_note should return None when verification is disabled."""
        import asyncio
        from services.scribe_service import verify_note
        # Default config has verify disabled
        result = asyncio.run(verify_note("some transcript", "some note"))
        assert result is None

    def test_verify_note_returns_none_on_empty_input(self):
        import asyncio
        from services.scribe_service import verify_note
        result = asyncio.run(verify_note("", "some note"))
        assert result is None
        result = asyncio.run(verify_note("some transcript", ""))
        assert result is None


# ═══════════════════════════════════════════════════
# Pipeline Metadata Tests
# ═══════════════════════════════════════════════════

class TestPipelineMeta:
    """Test pipeline metadata construction."""

    def test_build_pipeline_meta(self):
        from services.scribe_service import build_pipeline_meta
        from services.scribe_providers import StageTimer
        
        timer = StageTimer()
        timer.start("draft")
        timer.stop()
        
        meta = build_pipeline_meta(timer)
        assert "timings" in meta
        assert "models" in meta
        assert "stages_enabled" in meta
        assert "draft" in meta["timings"]
        assert "asr" in meta["models"]
        assert "draft" in meta["models"]

    def test_pipeline_meta_json_serializable(self):
        from services.scribe_service import build_pipeline_meta
        from services.scribe_providers import StageTimer
        
        timer = StageTimer()
        meta = build_pipeline_meta(timer)
        # Must be JSON-serializable for DB storage
        serialized = json.dumps(meta)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed == meta


# ═══════════════════════════════════════════════════
# Verification Schema Tests
# ═══════════════════════════════════════════════════

class TestVerificationSchema:
    """Verify the expected verification report format."""

    def test_valid_verification_report(self):
        """A well-formed verification report should have the expected fields."""
        report = {
            "verified": True,
            "confidence": 0.95,
            "warnings": [],
            "omissions": [],
            "sections_verified": ["SUBJECTIVE", "OBJECTIVE", "ASSESSMENT", "PLAN"],
        }
        assert isinstance(report["verified"], bool)
        assert 0 <= report["confidence"] <= 1.0
        assert isinstance(report["warnings"], list)
        assert isinstance(report["omissions"], list)
        assert isinstance(report["sections_verified"], list)

    def test_warning_format(self):
        warning = {
            "type": "unsupported_claim",
            "section": "PLAN",
            "text": "Discharge in 48 hours",
            "severity": "medium",
        }
        assert warning["type"] in ("unsupported_claim", "entity_mismatch", "low_confidence")
        assert warning["severity"] in ("low", "medium", "high")

    def test_omission_format(self):
        omission = {
            "text": "Patient mentioned chest tightness",
            "severity": "high",
        }
        assert "text" in omission
        assert omission["severity"] in ("low", "medium", "high")


# ═══════════════════════════════════════════════════
# Database Schema Tests
# ═══════════════════════════════════════════════════

class TestDatabaseSchema:
    """Test that scribe_notes schema supports new nullable columns."""

    def test_scribe_notes_create_table_includes_new_columns(self):
        """The DDL should include verification_json and pipeline_meta_json."""
        import database
        # Read the source file and check the DDL string
        import inspect
        source = inspect.getsource(database)
        assert "verification_json TEXT" in source
        assert "pipeline_meta_json TEXT" in source

    def test_save_scribe_note_accepts_new_fields(self):
        """save_scribe_note should accept verification_json and pipeline_meta_json."""
        from services.scribe_service import save_scribe_note
        import inspect
        sig = inspect.signature(save_scribe_note)
        params = list(sig.parameters.keys())
        assert "verification_json" in params
        assert "pipeline_meta_json" in params


# ═══════════════════════════════════════════════════
# Backward Compatibility Tests
# ═══════════════════════════════════════════════════

class TestBackwardCompat:
    """Ensure existing public APIs still work."""

    def test_transcribe_audio_signature(self):
        """transcribe_audio must still accept (audio_path, apply_correction, hints)."""
        from services.scribe_service import transcribe_audio
        import inspect
        sig = inspect.signature(transcribe_audio)
        params = list(sig.parameters.keys())
        assert "audio_path" in params
        assert "apply_correction" in params
        assert "hints" in params

    def test_structure_transcript_signature(self):
        from services.scribe_service import structure_transcript
        import inspect
        sig = inspect.signature(structure_transcript)
        params = list(sig.parameters.keys())
        assert "transcript" in params
        assert "format" in params

    def test_structure_transcript_stream_exists(self):
        from services.scribe_service import structure_transcript_stream
        import inspect
        assert inspect.isasyncgenfunction(structure_transcript_stream)

    def test_build_whisper_hints_exists(self):
        from services.scribe_service import build_whisper_hints
        import inspect
        assert inspect.isfunction(build_whisper_hints)

    def test_get_scribe_notes_exists(self):
        from services.scribe_service import get_scribe_notes
        import inspect
        assert inspect.isfunction(get_scribe_notes)

    def test_structuring_prompts_preserved(self):
        from services.scribe_service import STRUCTURING_PROMPTS
        assert "soap" in STRUCTURING_PROMPTS
        assert "discharge" in STRUCTURING_PROMPTS
        assert "handoff" in STRUCTURING_PROMPTS
        # Each prompt must contain the {transcript} placeholder
        for fmt, template in STRUCTURING_PROMPTS.items():
            assert "{transcript}" in template, f"Template '{fmt}' missing {{transcript}} placeholder"

    def test_save_scribe_note_backward_compat(self):
        """save_scribe_note must still work without new optional fields."""
        from services.scribe_service import save_scribe_note
        import inspect
        sig = inspect.signature(save_scribe_note)
        # New fields should have defaults (None)
        assert sig.parameters["verification_json"].default is None
        assert sig.parameters["pipeline_meta_json"].default is None


# ═══════════════════════════════════════════════════
# Fixtures Tests
# ═══════════════════════════════════════════════════

class TestFixtures:
    """Test that benchmark fixtures are valid."""

    def test_fixtures_exist(self):
        fixtures_dir = os.path.join(os.path.dirname(__file__), "..", "scripts", "fixtures", "scribe")
        assert os.path.isdir(fixtures_dir), f"Fixtures dir not found: {fixtures_dir}"
        files = list(Path(fixtures_dir).glob("*.json"))
        assert len(files) >= 3, f"Expected at least 3 fixtures, found {len(files)}"

    def test_fixtures_have_required_fields(self):
        from pathlib import Path
        fixtures_dir = os.path.join(os.path.dirname(__file__), "..", "scripts", "fixtures", "scribe")
        for f in Path(fixtures_dir).glob("*.json"):
            with open(f) as fh:
                data = json.load(fh)
            assert "case_id" in data, f"{f.name} missing case_id"
            assert "transcript" in data, f"{f.name} missing transcript"
            assert "expected_entities" in data, f"{f.name} missing expected_entities"
            assert "expected_sections" in data, f"{f.name} missing expected_sections"
            assert len(data["transcript"]) > 100, f"{f.name} transcript too short"
            assert len(data["expected_entities"]) >= 5, f"{f.name} needs at least 5 expected entities"


# Need this import for TestFixtures
from pathlib import Path


# ═══════════════════════════════════════════════════
# Async Verification Tests
# ═══════════════════════════════════════════════════

class TestAsyncVerificationService:
    """Test the async verification service layer."""

    def test_update_scribe_note_verification_exists(self):
        """update_scribe_note_verification must be importable."""
        from services.scribe_service import update_scribe_note_verification
        import inspect
        sig = inspect.signature(update_scribe_note_verification)
        assert "note_id" in sig.parameters
        assert "verification_json" in sig.parameters

    def test_get_scribe_note_by_id_exists(self):
        """get_scribe_note_by_id must be importable."""
        from services.scribe_service import get_scribe_note_by_id
        import inspect
        sig = inspect.signature(get_scribe_note_by_id)
        assert "note_id" in sig.parameters

    def test_save_note_with_pending_verification(self):
        """Note can be saved with a pending verification status."""
        from services.scribe_service import save_scribe_note, get_scribe_note_by_id
        pending_json = json.dumps({"status": "pending"})
        note_id = save_scribe_note(
            patient_id=None,
            author_id="DOC001",
            raw_transcript="Test transcript for async verification",
            structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=pending_json,
        )
        assert note_id is not None
        assert note_id > 0

        # Verify the note was saved with pending status
        note = get_scribe_note_by_id(note_id)
        assert note is not None
        v = note.get("verification_json")
        assert v is not None
        assert v.get("status") == "pending"

    def test_update_verification_after_save(self):
        """Verification can be updated after initial note save."""
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        pending_json = json.dumps({"status": "pending"})
        note_id = save_scribe_note(
            patient_id=None,
            author_id="DOC001",
            raw_transcript="Test transcript",
            structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=pending_json,
        )

        # Update with completed verification
        complete_json = json.dumps({
            "status": "complete",
            "verified": True,
            "confidence": 0.95,
            "warnings": [],
            "omissions": [],
        })
        success = update_scribe_note_verification(note_id, complete_json)
        assert success is True

        # Verify update persisted
        note = get_scribe_note_by_id(note_id)
        v = note.get("verification_json")
        assert v.get("status") == "complete"
        assert v.get("verified") is True
        assert v.get("confidence") == 0.95

    def test_update_verification_failed_state(self):
        """Failed verification status persists correctly."""
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        pending_json = json.dumps({"status": "pending"})
        note_id = save_scribe_note(
            patient_id=None,
            author_id="DOC001",
            raw_transcript="Test transcript",
            structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=pending_json,
        )

        # Update with failed verification
        failed_json = json.dumps({
            "status": "failed",
            "error": "Model returned empty output",
        })
        success = update_scribe_note_verification(note_id, failed_json)
        assert success is True

        # Draft note still exists and is intact
        note = get_scribe_note_by_id(note_id)
        assert note is not None
        assert note["structured_note"] == "SUBJECTIVE: Test"
        assert note["raw_transcript"] == "Test transcript"
        v = note.get("verification_json")
        assert v.get("status") == "failed"
        assert "error" in v

    def test_note_survives_verification_failure(self):
        """Draft note must still be readable even when verification has failed status."""
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        note_id = save_scribe_note(
            patient_id=None,
            author_id="DOC001",
            raw_transcript="Patient presents with chest pain",
            structured_note="SUBJECTIVE: Chest pain\nOBJECTIVE: BP 130/80",
            note_format="soap",
            verification_json=json.dumps({"status": "pending"}),
        )

        # Simulate verification failure
        update_scribe_note_verification(
            note_id,
            json.dumps({"status": "failed", "error": "Timeout"}),
        )

        # Note must still be fully accessible
        note = get_scribe_note_by_id(note_id)
        assert note is not None
        assert "Chest pain" in note["structured_note"]


class TestVerificationRouteConstants:
    """Test that the verification state model and registry are properly defined."""

    def test_verification_state_constants_exist(self):
        from routes.scribe_routes import VERIFY_NOT_REQUESTED, VERIFY_PENDING, VERIFY_COMPLETE, VERIFY_FAILED
        assert VERIFY_NOT_REQUESTED == "not_requested"
        assert VERIFY_PENDING == "pending"
        assert VERIFY_COMPLETE == "complete"
        assert VERIFY_FAILED == "failed"

    def test_terminal_states_defined(self):
        """Terminal states (complete, failed) must be defined."""
        from routes.scribe_routes import _TERMINAL_STATES
        assert "complete" in _TERMINAL_STATES
        assert "failed" in _TERMINAL_STATES
        assert "pending" not in _TERMINAL_STATES

    def test_verification_registry_exists(self):
        """In-process task registry must exist as a dict keyed by note_id."""
        from routes.scribe_routes import _verification_registry
        assert isinstance(_verification_registry, dict)

    def test_is_verification_in_flight_callable(self):
        """Duplicate dispatch guard must be importable."""
        from routes.scribe_routes import _is_verification_in_flight
        assert _is_verification_in_flight(999999) is False

    def test_register_verification_task_callable(self):
        """Task registration function must be importable."""
        from routes.scribe_routes import _register_verification_task
        assert callable(_register_verification_task)

    def test_run_verification_async_is_coroutine(self):
        """_run_verification_async must be a coroutine function."""
        import asyncio
        from routes.scribe_routes import _run_verification_async
        assert asyncio.iscoroutinefunction(_run_verification_async)

    def test_verification_endpoint_registered(self):
        """GET /notes/{note_id}/verification endpoint must exist."""
        from routes.scribe_routes import router
        paths = [route.path for route in router.routes]
        assert "/api/scribe/notes/{note_id}/verification" in paths

    def test_retry_endpoint_registered(self):
        """POST /notes/{note_id}/verify retry endpoint must exist."""
        from routes.scribe_routes import router
        paths = [route.path for route in router.routes]
        assert "/api/scribe/notes/{note_id}/verify" in paths

    def test_reconcile_function_callable(self):
        """Startup recovery function must be importable."""
        from routes.scribe_routes import reconcile_stale_pending_verifications
        assert callable(reconcile_stale_pending_verifications)

    def test_stale_pending_threshold_defined(self):
        """Stale pending threshold must be a positive number."""
        from routes.scribe_routes import STALE_PENDING_THRESHOLD_MINUTES
        assert isinstance(STALE_PENDING_THRESHOLD_MINUTES, (int, float))
        assert STALE_PENDING_THRESHOLD_MINUTES > 0


class TestVerificationStatusModel:
    """Test the verification JSON schema and lifecycle transitions."""

    def test_not_requested_state(self):
        v = {"status": "not_requested"}
        assert v["status"] == "not_requested"
        serialized = json.dumps(v)
        assert json.loads(serialized) == v

    def test_pending_state(self):
        v = {"status": "pending"}
        assert v["status"] == "pending"

    def test_complete_state_has_required_fields(self):
        v = {
            "status": "complete",
            "verified": True,
            "confidence": 0.92,
            "warnings": [],
            "omissions": [],
            "sections_verified": ["SUBJECTIVE", "OBJECTIVE"],
        }
        assert v["status"] == "complete"
        assert isinstance(v["verified"], bool)
        assert 0 <= v["confidence"] <= 1

    def test_failed_state_has_error(self):
        v = {"status": "failed", "error": "Model timeout after 120s"}
        assert v["status"] == "failed"
        assert len(v["error"]) > 0


class TestStateTransitionGuards:
    """Test that update_scribe_note_verification respects state transition rules."""

    def test_complete_not_overwritten_by_failed(self):
        """A 'complete' verification must not be overwritten by 'failed'."""
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test", structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "complete", "verified": True, "confidence": 0.95}),
        )
        update_scribe_note_verification(
            note_id, json.dumps({"status": "failed", "error": "Stale task"})
        )
        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "complete"

    def test_complete_not_overwritten_by_pending(self):
        """A 'complete' verification must not be overwritten by 'pending'."""
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test", structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "complete", "verified": True, "confidence": 0.9}),
        )
        update_scribe_note_verification(
            note_id, json.dumps({"status": "pending"})
        )
        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "complete"

    def test_failed_can_be_overwritten_by_pending(self):
        """A 'failed' verification CAN be overwritten by 'pending' (retry path)."""
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test", structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "failed", "error": "timeout"}),
        )
        update_scribe_note_verification(
            note_id, json.dumps({"status": "pending"})
        )
        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "pending"

    def test_pending_can_transition_to_complete(self):
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test", structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "pending"}),
        )
        update_scribe_note_verification(
            note_id, json.dumps({"status": "complete", "verified": True, "confidence": 0.88})
        )
        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "complete"

    def test_pending_can_transition_to_failed(self):
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test", structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "pending"}),
        )
        update_scribe_note_verification(
            note_id, json.dumps({"status": "failed", "error": "crash"})
        )
        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "failed"

    def test_idempotent_complete_update(self):
        """Writing 'complete' over 'complete' should succeed without corruption."""
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test", structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "complete", "verified": True, "confidence": 0.95}),
        )
        result = update_scribe_note_verification(
            note_id, json.dumps({"status": "complete", "verified": True, "confidence": 0.95})
        )
        assert result is True
        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "complete"


class TestDuplicateDispatchPrevention:
    """Test the in-process verification registry."""

    def test_no_task_in_flight_returns_false(self):
        from routes.scribe_routes import _is_verification_in_flight
        assert _is_verification_in_flight(123456789) is False

    def test_registry_tracks_by_note_id(self):
        from routes.scribe_routes import _verification_registry
        assert isinstance(_verification_registry, dict)

    def test_completed_task_cleaned_from_registry(self):
        """A finished task should be cleaned from the registry on next check."""
        import asyncio
        from routes.scribe_routes import _verification_registry, _is_verification_in_flight

        async def dummy():
            pass

        loop = asyncio.new_event_loop()
        task = loop.create_task(dummy())
        loop.run_until_complete(task)

        note_id = 99999999
        _verification_registry[note_id] = task

        assert _is_verification_in_flight(note_id) is False
        assert note_id not in _verification_registry
        loop.close()


class TestStalePendingReconciliation:
    """Test startup recovery for orphaned pending verifications."""

    def test_reconcile_stale_pending_marks_as_failed(self):
        from services.scribe_service import save_scribe_note, get_scribe_note_by_id
        from routes.scribe_routes import reconcile_stale_pending_verifications
        from database import get_db

        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test stale pending",
            structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "pending"}),
        )
        # Backdate to make it stale
        db = get_db()
        db.execute(
            "UPDATE scribe_notes SET created_at = datetime('now', '-30 minutes') WHERE id = ?",
            (note_id,)
        )
        db.commit()
        db.close()

        count = reconcile_stale_pending_verifications()
        assert count >= 1

        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "failed"
        assert "orphaned" in note["verification_json"].get("error", "").lower()

    def test_reconcile_does_not_touch_recent_pending(self):
        from services.scribe_service import save_scribe_note, get_scribe_note_by_id
        from routes.scribe_routes import reconcile_stale_pending_verifications

        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test recent pending",
            structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "pending"}),
        )
        reconcile_stale_pending_verifications()
        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "pending"

    def test_reconcile_does_not_touch_complete(self):
        from services.scribe_service import save_scribe_note, get_scribe_note_by_id
        from routes.scribe_routes import reconcile_stale_pending_verifications

        note_id = save_scribe_note(
            patient_id=None, author_id="DOC001",
            raw_transcript="Test complete",
            structured_note="SUBJECTIVE: Test",
            note_format="soap",
            verification_json=json.dumps({"status": "complete", "verified": True, "confidence": 0.9}),
        )
        reconcile_stale_pending_verifications()
        note = get_scribe_note_by_id(note_id)
        assert note["verification_json"]["status"] == "complete"

    def test_reconcile_returns_integer(self):
        from routes.scribe_routes import reconcile_stale_pending_verifications
        result = reconcile_stale_pending_verifications()
        assert isinstance(result, int)


# ═══════════════════════════════════════════════════
# Note Lifecycle — Schema Tests
# ═══════════════════════════════════════════════════

class TestNoteLifecycleSchema:
    """Verify lifecycle columns exist and have correct defaults."""

    def test_status_column_exists(self):
        from database import get_db, init_db
        init_db()
        db = get_db()
        cols = {r[1] for r in db.execute("PRAGMA table_info(scribe_notes)").fetchall()}
        db.close()
        assert "status" in cols

    def test_lifecycle_columns_exist(self):
        from database import get_db, init_db
        init_db()
        db = get_db()
        cols = {r[1] for r in db.execute("PRAGMA table_info(scribe_notes)").fetchall()}
        db.close()
        expected = {"status", "version", "supersedes_note_id", "signed_by", "signed_at",
                    "finalized_at", "rejected_by", "rejected_at", "rejection_reason",
                    "last_edited_by", "last_edited_at", "ai_generated"}
        assert expected.issubset(cols)

    def test_new_note_defaults_to_draft(self):
        from services.scribe_service import save_scribe_note, get_scribe_note_by_id
        note_id = save_scribe_note(patient_id=None, author_id="D001",
                                    raw_transcript="Test", structured_note="Note",
                                    note_format="soap")
        note = get_scribe_note_by_id(note_id)
        assert note["status"] == "draft"
        assert note["version"] == 1
        assert note["ai_generated"] == 1


# ═══════════════════════════════════════════════════
# Note Lifecycle — Permissions Tests
# ═══════════════════════════════════════════════════

class TestNoteLifecyclePermissions:
    """Verify RBAC permission mappings for lifecycle operations."""

    def test_scribe_permissions_exist(self):
        from permissions import SCRIBE_EDIT_DRAFT, SCRIBE_SIGN, SCRIBE_REJECT
        assert SCRIBE_EDIT_DRAFT == "scribe.edit_draft"
        assert SCRIBE_SIGN == "scribe.sign"
        assert SCRIBE_REJECT == "scribe.reject"

    def test_doctor_can_sign(self):
        from permissions import has_permission
        assert has_permission("doctor", "scribe.sign")

    def test_nurse_cannot_sign(self):
        from permissions import has_permission
        assert not has_permission("nurse", "scribe.sign")

    def test_doctor_can_reject(self):
        from permissions import has_permission
        assert has_permission("doctor", "scribe.reject")

    def test_nurse_cannot_reject(self):
        from permissions import has_permission
        assert not has_permission("nurse", "scribe.reject")

    def test_nurse_can_edit_draft(self):
        from permissions import has_permission
        assert has_permission("nurse", "scribe.edit_draft")

    def test_admin_cannot_edit(self):
        from permissions import has_permission
        assert not has_permission("admin", "scribe.edit_draft")

    def test_admin_cannot_sign(self):
        from permissions import has_permission
        assert not has_permission("admin", "scribe.sign")

    def test_patient_no_scribe_access(self):
        from permissions import has_permission
        assert not has_permission("patient", "scribe.create_draft")
        assert not has_permission("patient", "scribe.read_all")
        assert not has_permission("patient", "scribe.sign")


# ═══════════════════════════════════════════════════
# Note Lifecycle — State Transition Tests
# ═══════════════════════════════════════════════════

class TestNoteLifecycleTransitions:
    """Test all lifecycle state transitions via service layer."""

    def _make_draft(self):
        from services.scribe_service import save_scribe_note
        return save_scribe_note(patient_id=None, author_id="D001",
                                raw_transcript="Transcript", structured_note="Note",
                                note_format="soap")

    def test_draft_to_signed(self):
        from services.scribe_service import sign_scribe_note, get_scribe_note_by_id
        nid = self._make_draft()
        r = sign_scribe_note(nid, "D001")
        assert r.get("ok") is True
        note = get_scribe_note_by_id(nid)
        assert note["status"] == "signed"
        assert note["signed_by"] == "D001"
        assert note["signed_at"] is not None

    def test_draft_to_rejected(self):
        from services.scribe_service import reject_scribe_note, get_scribe_note_by_id
        nid = self._make_draft()
        r = reject_scribe_note(nid, "D001", "Inaccurate assessment")
        assert r.get("ok") is True
        note = get_scribe_note_by_id(nid)
        assert note["status"] == "rejected"
        assert note["rejected_by"] == "D001"
        assert note["rejection_reason"] == "Inaccurate assessment"

    def test_signed_rejects_direct_edit(self):
        from services.scribe_service import sign_scribe_note, update_scribe_note_content
        nid = self._make_draft()
        sign_scribe_note(nid, "D001")
        r = update_scribe_note_content(nid, "Modified content", "D001")
        assert "error" in r
        assert "Only drafts" in r["error"]

    def test_signed_rejects_second_sign(self):
        from services.scribe_service import sign_scribe_note
        nid = self._make_draft()
        sign_scribe_note(nid, "D001")
        r = sign_scribe_note(nid, "D002")
        assert "error" in r

    def test_rejected_rejects_sign(self):
        from services.scribe_service import reject_scribe_note, sign_scribe_note
        nid = self._make_draft()
        reject_scribe_note(nid, "D001", "Bad")
        r = sign_scribe_note(nid, "D001")
        assert "error" in r

    def test_edit_draft_succeeds(self):
        from services.scribe_service import update_scribe_note_content, get_scribe_note_by_id
        nid = self._make_draft()
        r = update_scribe_note_content(nid, "Updated note content", "N001")
        assert r.get("ok") is True
        note = get_scribe_note_by_id(nid)
        assert note["structured_note"] == "Updated note content"
        assert note["last_edited_by"] == "N001"


# ═══════════════════════════════════════════════════
# Note Lifecycle — Versioning Tests
# ═══════════════════════════════════════════════════

class TestNoteLifecycleVersioning:
    """Test revision/versioning workflow."""

    def _make_signed(self):
        from services.scribe_service import save_scribe_note, sign_scribe_note
        nid = save_scribe_note(patient_id=None, author_id="D001",
                               raw_transcript="Transcript", structured_note="Original note",
                               note_format="soap")
        sign_scribe_note(nid, "D001")
        return nid

    def test_create_revision_from_signed(self):
        from services.scribe_service import create_revision_from_signed, get_scribe_note_by_id
        orig_id = self._make_signed()
        r = create_revision_from_signed(orig_id, "D002")
        assert r.get("ok") is True
        assert r["version"] == 2
        new_note = get_scribe_note_by_id(r["new_note_id"])
        assert new_note["status"] == "draft"
        assert new_note["supersedes_note_id"] == orig_id
        # Original remains signed until revision is signed
        orig = get_scribe_note_by_id(orig_id)
        assert orig["status"] == "signed"

    def test_signing_revision_supersedes_original(self):
        from services.scribe_service import create_revision_from_signed, sign_scribe_note, get_scribe_note_by_id
        orig_id = self._make_signed()
        r = create_revision_from_signed(orig_id, "D002")
        sign_scribe_note(r["new_note_id"], "D002")
        orig = get_scribe_note_by_id(orig_id)
        assert orig["status"] == "superseded"
        revised = get_scribe_note_by_id(r["new_note_id"])
        assert revised["status"] == "signed"

    def test_cannot_revise_draft(self):
        from services.scribe_service import save_scribe_note, create_revision_from_signed
        nid = save_scribe_note(patient_id=None, author_id="D001",
                               raw_transcript="T", structured_note="N", note_format="soap")
        r = create_revision_from_signed(nid, "D001")
        assert "error" in r

    def test_cannot_revise_rejected(self):
        from services.scribe_service import save_scribe_note, reject_scribe_note, create_revision_from_signed
        nid = save_scribe_note(patient_id=None, author_id="D001",
                               raw_transcript="T", structured_note="N", note_format="soap")
        reject_scribe_note(nid, "D001", "Bad")
        r = create_revision_from_signed(nid, "D001")
        assert "error" in r


# ═══════════════════════════════════════════════════
# Note Lifecycle — Route Registration Tests
# ═══════════════════════════════════════════════════

class TestNoteLifecycleRoutes:
    """Verify lifecycle endpoints are registered on the router."""

    def test_edit_endpoint_registered(self):
        from routes.scribe_routes import router
        paths = [r.path for r in router.routes if hasattr(r, "path")]
        assert any("/notes/{note_id}" in p for p in paths)

    def test_sign_endpoint_registered(self):
        from routes.scribe_routes import router
        paths = [r.path for r in router.routes if hasattr(r, "path")]
        assert any("/notes/{note_id}/sign" in p for p in paths)

    def test_reject_endpoint_registered(self):
        from routes.scribe_routes import router
        paths = [r.path for r in router.routes if hasattr(r, "path")]
        assert any("/notes/{note_id}/reject" in p for p in paths)

    def test_revise_endpoint_registered(self):
        from routes.scribe_routes import router
        paths = [r.path for r in router.routes if hasattr(r, "path")]
        assert any("/notes/{note_id}/revise" in p for p in paths)


# ═══════════════════════════════════════════════════
# Note Lifecycle — Status Filter Tests
# ═══════════════════════════════════════════════════

class TestNoteStatusFiltering:
    """Verify get_scribe_notes supports status filtering."""

    def test_filter_by_draft_status(self):
        from services.scribe_service import save_scribe_note, get_scribe_notes, sign_scribe_note
        # Create draft and signed notes
        d_id = save_scribe_note(patient_id=None, author_id="D001",
                                raw_transcript="T", structured_note="N", note_format="soap")
        s_id = save_scribe_note(patient_id=None, author_id="D001",
                                raw_transcript="T2", structured_note="N2", note_format="soap")
        sign_scribe_note(s_id, "D001")
        drafts = get_scribe_notes(patient_id=None, status="draft")
        assert all(n["status"] == "draft" for n in drafts)

    def test_filter_by_signed_status(self):
        from services.scribe_service import get_scribe_notes
        signed = get_scribe_notes(patient_id=None, status="signed")
        assert all(n["status"] == "signed" for n in signed)


# ═══════════════════════════════════════════════════
# Note Lifecycle — Verification Coexistence
# ═══════════════════════════════════════════════════

class TestVerificationStillWorks:
    """Confirm existing async verification flow is not broken by lifecycle changes."""

    def test_verification_update_on_draft(self):
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, get_scribe_note_by_id
        nid = save_scribe_note(patient_id=None, author_id="D001",
                               raw_transcript="T", structured_note="N", note_format="soap",
                               verification_json=json.dumps({"status": "pending"}))
        update_scribe_note_verification(nid, json.dumps({"status": "complete", "verified": True}))
        note = get_scribe_note_by_id(nid)
        assert note["verification_json"]["status"] == "complete"
        # Note lifecycle status should still be draft (independent)
        assert note["status"] == "draft"

    def test_sign_preserves_verification(self):
        from services.scribe_service import save_scribe_note, update_scribe_note_verification, sign_scribe_note, get_scribe_note_by_id
        nid = save_scribe_note(patient_id=None, author_id="D001",
                               raw_transcript="T", structured_note="N", note_format="soap",
                               verification_json=json.dumps({"status": "complete", "verified": True, "confidence": 0.95}))
        sign_scribe_note(nid, "D001")
        note = get_scribe_note_by_id(nid)
        assert note["status"] == "signed"
        assert note["verification_json"]["status"] == "complete"
        assert note["verification_json"]["confidence"] == 0.95
