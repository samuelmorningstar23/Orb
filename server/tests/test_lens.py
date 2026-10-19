import pytest
import os
import json
import base64
from unittest import mock
import httpx
from services.lens_service import analyze_image_stream, analyze_image_full
import config

# A 1x1 transparent PNG as dummy image
DUMMY_IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

@pytest.mark.asyncio
async def test_lens_service_calls_correct_model():
    """Verify that analyze_image_stream calls Ollama with the configured VISION_MODEL.

    Asserted against config.VISION_MODEL (not a literal): since the LLM
    auto-fallback landed, LLM_MODEL/VISION_MODEL resolve at import time to a
    locally-pulled model when the configured one is absent, so the literal
    varies per machine while the invariant (Lens uses the shared configured
    model) does not.
    """
    # Create a dummy image file
    dummy_image_path = "/tmp/test_lens_image.png"
    with open(dummy_image_path, "wb") as f:
        f.write(DUMMY_IMAGE_BYTES)

    try:
        # Mock responses from Ollama stream
        mock_response_lines = [
            b'{"message": {"content": "FINDINGS:"}}',
            b'{"message": {"content": "\\n- Normal cardiac silhouette."}}',
            b'{"message": {"content": "\\n\\nIMPRESSION:"}}',
            b'{"message": {"content": "\\n- No acute cardiopulmonary findings."}}',
            b'{"done": true}',
        ]

        class MockResponse:
            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                for line in mock_response_lines:
                    yield line

            async def aclose(self):
                pass

        # Context manager mock for stream
        class MockAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            def stream(self, method, url, **kwargs):
                self.method = method
                self.url = url
                self.kwargs = kwargs
                
                # Assertions on the request parameters
                assert method == "POST"
                assert url.endswith("/api/chat")
                
                json_payload = kwargs.get("json", {})
                assert json_payload.get("model") == config.VISION_MODEL
                assert "messages" in json_payload
                assert "keep_alive" in json_payload, "keep_alive must be set for model retention"
                assert "options" in json_payload, "options must be set for inference params"
                
                # Check system prompt presence
                messages = json_payload["messages"]
                assert messages[0]["role"] == "system"
                assert "FINDINGS:" in messages[0]["content"]
                
                # Check user image content
                user_msg = messages[1]
                assert user_msg["role"] == "user"
                assert "images" in user_msg
                assert len(user_msg["images"]) == 1
                
                class StreamContext:
                    async def __aenter__(self):
                        return MockResponse()
                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        pass
                return StreamContext()

        with mock.patch("httpx.AsyncClient", side_effect=MockAsyncClient):
            # Run stream analysis
            chunks = []
            async for chunk in analyze_image_stream(
                dummy_image_path,
                clinical_context="Assess heart size",
                patient_context="Age 45",
            ):
                chunks.append(chunk)

            full_text = "".join(chunks)
            assert "FINDINGS:" in full_text
            assert "IMPRESSION:" in full_text
            assert "Normal cardiac silhouette" in full_text

            # Test full analysis wrapper
            full_analysis = await analyze_image_full(
                dummy_image_path,
                clinical_context="Assess heart size",
                patient_context="Age 45",
            )
            assert full_analysis == full_text

    finally:
        if os.path.exists(dummy_image_path):
            os.remove(dummy_image_path)


def test_single_shared_model_instance():
    """Verify LLM_MODEL and VISION_MODEL resolve to the same model tag.
    
    This is the architectural guarantee: Sage and Lens use one shared
    medgemma:27b instance (~17GB VRAM) instead of two separate instances.
    """
    assert config.LLM_MODEL == config.VISION_MODEL, (
        f"VRAM WASTE: LLM_MODEL ({config.LLM_MODEL}) != VISION_MODEL ({config.VISION_MODEL}). "
        f"Both must point to the same model tag to share a single Ollama instance."
    )
    assert "medgemma" in config.LLM_MODEL.lower(), (
        f"Expected MedGemma model, got: {config.LLM_MODEL}"
    )
