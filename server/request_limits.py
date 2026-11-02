"""Bound HTTP request bodies before JSON or multipart parsers consume them.

Content-Length is only an early refusal: streamed and incorrectly declared
bodies must meet the same limit. Counting ASGI receive messages avoids buffering
the request and leaves response streams and Socket.IO traffic alone.
"""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


DEFAULT_JSON_BODY_BYTES = 512 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_AUDIO_BYTES = 100 * 1024 * 1024
# Room for multipart boundaries and small form fields around each file.
MULTIPART_FRAMING_BYTES = 1024 * 1024
DEFAULT_MULTIPART_BODY_BYTES = MAX_AUDIO_BYTES + MULTIPART_FRAMING_BYTES


class _BodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, json_limit: int = DEFAULT_JSON_BODY_BYTES,
                 multipart_limit: int = DEFAULT_MULTIPART_BODY_BYTES,
                 multipart_limits: dict[str, int] | None = None) -> None:
        self.multipart_limits = dict(multipart_limits or {})
        if (json_limit <= 0 or multipart_limit <= 0
                or any(value <= 0 for value in self.multipart_limits.values())):
            raise ValueError("Request body limits must be positive")
        self.app = app
        self.json_limit = json_limit
        self.multipart_limit = multipart_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        content_type = next((value for key, value in headers
                             if key.lower() == b"content-type"), b"")
        multipart = content_type.strip().lower().startswith(b"multipart/")
        limit = (self.multipart_limits.get(scope["path"].rstrip("/") or "/",
                                           self.multipart_limit)
                 if multipart else self.json_limit)
        lengths = [value.strip() for key, value in headers
                   if key.lower() == b"content-length"]
        if lengths:
            # Reject signs, empty/non-numeric values and conflicting duplicates.
            # Avoid int() on attacker-sized digit strings as well.
            normalized = [value.lstrip(b"0") or b"0" for value in lengths]
            if (any(not value.isdigit() for value in lengths)
                    or len(set(normalized)) != 1):
                await JSONResponse(status_code=400, content={
                    "detail": "Invalid Content-Length"})(scope, receive, send)
                return
            ceiling = str(limit).encode("ascii")
            declared = normalized[0]
            if (len(declared) > len(ceiling)
                    or (len(declared) == len(ceiling) and declared > ceiling)):
                await JSONResponse(status_code=413, content={
                    "detail": "Request body too large"})(scope, receive, send)
                return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge()
            return message

        async def reject() -> None:
            nonlocal response_started
            response_started = True
            await JSONResponse(status_code=413, content={
                "detail": "Request body too large"})(scope, receive, send)

        async def limited_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                # Some parsers translate receive errors into their own 400/500.
                # Preserve the transport limit's 413 and send its body once.
                if not response_started:
                    await reject()
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _BodyTooLarge:
            if response_started:
                raise
            await reject()
