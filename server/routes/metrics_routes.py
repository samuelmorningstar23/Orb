"""Orb Hospital OS — Prometheus metrics endpoint.

Exposes GET /metrics in the Prometheus text exposition format, rendered by
services.metrics_service (which is dependency-free — no prometheus_client).

SECURITY / AUTH:
  /metrics is conventionally scraped UNAUTHENTICATED by a Prometheus server, and
  Orb's body contains only coarse operational counts (uptime, DB size, number of
  patients/orders/audit rows, LLM queue depth) — never PHI. We therefore leave it
  open by default so a standard scrape config works out of the box.

  If you want to gate it (recommended when the endpoint is reachable off-host),
  set ORB_METRICS_TOKEN and scrape with `Authorization: Bearer <token>`. The
  check below is intentionally trivial and only engages when the env var is set,
  so it stays zero-config for the common in-cluster case.
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import PlainTextResponse

from services.metrics_service import render_metrics, CONTENT_TYPE

router = APIRouter(tags=["metrics"])


def _authorized(authorization: str | None) -> bool:
    """Return True if the request may read /metrics.

    Open unless ORB_METRICS_TOKEN is configured; when set, require a matching
    bearer token (timing-safe compare).
    """
    token = os.getenv("ORB_METRICS_TOKEN")
    if not token:
        return True
    if not authorization or not authorization.startswith("Bearer "):
        return False
    presented = authorization[len("Bearer "):].strip()
    return hmac.compare_digest(presented, token)


@router.get("/metrics")
def metrics(authorization: str | None = Header(default=None)):
    """Prometheus scrape endpoint. Returns text/plain exposition."""
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return PlainTextResponse(content=render_metrics(), media_type=CONTENT_TYPE)
