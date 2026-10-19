"""Streaming-safe HTTP security headers, independent of application startup."""

from collections.abc import Awaitable, Callable
from typing import Any

# ASGI messages carry heterogeneous protocol fields; no framework imports.
type Scope = dict[str, Any]
type Message = dict[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"content-security-policy", b"frame-ancestors 'self'; object-src 'none'; base-uri 'self'"),
)
_HSTS = (b"strict-transport-security", b"max-age=31536000")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        forwarded = dict(scope.get("headers") or []).get(b"x-forwarded-proto", b"")
        https = (
            scope.get("scheme") == "https" or forwarded.split(b",")[0].strip().lower() == b"https"
        )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for name, value in _SECURITY_HEADERS + ((_HSTS,) if https else ()):
                    # A route that set its own value keeps it.
                    if name not in present:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
