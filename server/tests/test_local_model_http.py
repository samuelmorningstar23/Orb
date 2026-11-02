"""Reuse trust roots without reusing any patient's HTTP client state."""

import ssl
import threading
from concurrent.futures import ThreadPoolExecutor

import certifi
import httpx
import pytest

from services import local_model_http


def test_trust_policy_matches_httpx_verified_default(monkeypatch):
    monkeypatch.setattr(local_model_http, "_context", None)
    context = local_model_http.local_model_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    expected = ssl.create_default_context(cafile=certifi.where())
    assert context.get_ca_certs(binary_form=True) == expected.get_ca_certs(binary_form=True)


def test_concurrent_first_use_constructs_exactly_one_context(monkeypatch):
    context = ssl.create_default_context(cafile=certifi.where())
    calls = []
    monkeypatch.setattr(local_model_http, "_context", None)

    def construct(*, cafile):
        calls.append(cafile)
        return context

    monkeypatch.setattr(local_model_http.ssl, "create_default_context", construct)
    barrier = threading.Barrier(16)

    def get_context(_):
        barrier.wait(timeout=10)
        return local_model_http.local_model_ssl_context()

    with ThreadPoolExecutor(max_workers=16) as executor:
        contexts = list(executor.map(get_context, range(16)))
    assert all(item is context for item in contexts)
    assert calls == [certifi.where()]


@pytest.mark.asyncio
async def test_clients_with_shared_trust_context_do_not_share_cookies_or_headers():
    seen = []

    def respond(request):
        seen.append((request.headers.get("cookie"), request.headers.get("x-patient")))
        return httpx.Response(200, headers={"set-cookie": "session=patient-one; Path=/"})

    for headers in ({"X-Patient": "one"}, {}):
        async with httpx.AsyncClient(
            verify=local_model_http.local_model_ssl_context(),
            trust_env=False,
            headers=headers,
            transport=httpx.MockTransport(respond),
        ) as client:
            await client.get("http://127.0.0.1:11435/api/ps")
    assert seen == [(None, "one"), (None, None)]


def test_failed_first_construction_is_retryable(monkeypatch):
    context = ssl.create_default_context(cafile=certifi.where())
    monkeypatch.setattr(local_model_http, "_context", None)
    calls = []

    def construct(*, cafile):
        calls.append(cafile)
        if len(calls) == 1:
            raise OSError("synthetic CA read failure")
        return context

    monkeypatch.setattr(local_model_http.ssl, "create_default_context", construct)
    with pytest.raises(OSError, match="synthetic CA read failure"):
        local_model_http.local_model_ssl_context()
    assert local_model_http.local_model_ssl_context() is context
    assert len(calls) == 2
