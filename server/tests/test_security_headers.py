"""Orb sets its own security headers, on the web app and on the API alike.

It set none: no nosniff, no Referrer-Policy (URLs carry patient ids), nothing
stopping another site framing the app, and no HSTS. HSTS is sent only on a
connection that is already HTTPS, so a dev box on http://localhost is never
pinned to HTTPS.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

_ALWAYS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "SAMEORIGIN",
}


def _assert_headers(resp):
    for name, value in _ALWAYS.items():
        assert resp.headers.get(name) == value, (name, dict(resp.headers))
    csp = resp.headers.get("content-security-policy", "")
    assert "frame-ancestors 'self'" in csp
    assert "object-src 'none'" in csp


def test_api_route_carries_the_headers():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    _assert_headers(resp)
    assert "strict-transport-security" not in resp.headers


def test_an_unauthenticated_api_refusal_carries_them_too():
    resp = client.get("/api/auth/me")
    assert resp.status_code in (401, 403)
    _assert_headers(resp)


@pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "dist" / "index.html").exists(),
                    reason="no built web app to serve")
def test_html_route_carries_the_headers():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    _assert_headers(resp)


def test_hsts_only_over_https():
    plain = client.get("/api/health")
    assert "strict-transport-security" not in plain.headers
    proxied = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert proxied.headers.get("strict-transport-security", "").startswith("max-age=")
    direct = TestClient(app, base_url="https://testserver").get("/api/health")
    assert direct.headers.get("strict-transport-security", "").startswith("max-age=")
