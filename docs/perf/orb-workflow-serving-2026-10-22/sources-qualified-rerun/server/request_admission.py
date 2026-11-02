"""Bound live API work without buffering response streams or waiting in a queue.

One ASGI process owns the counter. Increasing uvicorn workers multiplies this
limit, so the hospital profile deliberately runs one application process.
Static assets and cheap liveness probes remain available during overload.
"""

from __future__ import annotations

from services.metrics_service import inc, set_gauge
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestAdmissionMiddleware:
    def __init__(self, app: ASGIApp, *, max_inflight: int = 200) -> None:
        if type(max_inflight) is not int or not 1 <= max_inflight <= 2000:
            raise ValueError("HTTP admission limit must be between 1 and 2000")
        self.app = app
        self.max_inflight = max_inflight
        self.active = 0
        self.peak = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or not path.startswith("/api/")
            or (
                scope.get("method") in ("GET", "HEAD")
                and path
                in (
                    "/api/health",
                    "/api/health/loop",
                )
            )
            or scope.get("method") == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return
        # No await between the check and reservation: atomic on this event loop.
        if self.active >= self.max_inflight:
            inc("orb_http_admission_rejected_total")
            await JSONResponse(
                status_code=503,
                content={
                    "detail": "Orb is busy. Please retry shortly.",
                    "code": "capacity_busy",
                },
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        self.active += 1
        self.peak = max(self.peak, self.active)
        set_gauge("orb_http_inflight", self.active)
        set_gauge("orb_http_inflight_peak", self.peak)
        set_gauge("orb_http_inflight_limit", self.max_inflight)
        try:
            # Await the whole ASGI application, including the final SSE chunk
            # and disconnect cleanup. Never materialize the response body.
            await self.app(scope, receive, send)
        finally:
            self.active -= 1
            set_gauge("orb_http_inflight", self.active)
