"""Actual overlapping ASGI calls, including long-lived response streams."""

import asyncio

import pytest

from request_admission import RequestAdmissionMiddleware


def scope(path="/api/test", method="GET"):
    return {"type": "http", "path": path, "method": method, "headers": []}


async def receive():
    return {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_200_overlapping_streams_hold_slots_and_reject_201_without_buffering():
    gate = asyncio.Event()
    started = 0
    messages = []

    async def application(s, r, send):
        nonlocal started
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        started += 1
        await gate.wait()
        await send({"type": "http.response.body", "body": b"last"})

    async def send(message):
        messages.append(message)

    middleware = RequestAdmissionMiddleware(application, max_inflight=200)
    tasks = [asyncio.create_task(middleware(scope(), receive, send)) for _ in range(200)]
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            if started == 200:
                break
        assert started == middleware.active == middleware.peak == 200
        assert sum(m.get("body") == b"first" for m in messages) == 200
        rejected = []

        async def reject_send(message):
            rejected.append(message)

        await middleware(scope(), receive, reject_send)
        assert rejected[0]["status"] == 503
        assert (b"retry-after", b"1") in rejected[0]["headers"]
        assert middleware.active == 200
    finally:
        gate.set()
        await asyncio.gather(*tasks)
    assert middleware.active == 0
    assert sum(m.get("body") == b"last" for m in messages) == 200


@pytest.mark.asyncio
async def test_cancel_and_exception_release_only_owned_request():
    gate = asyncio.Event()

    async def app(s, r, send):
        if s["path"].endswith("error"):
            raise RuntimeError("test")
        await gate.wait()

    async def send(message):
        pass

    middleware = RequestAdmissionMiddleware(app, max_inflight=2)
    task = asyncio.create_task(middleware(scope(), receive, send))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError):
        await middleware(scope("/api/error"), receive, send)
    assert middleware.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert middleware.active == 0


@pytest.mark.asyncio
async def test_health_static_and_preflight_do_not_consume_admission():
    async def app(s, r, send):
        assert middleware.active == 1

    async def send(message):
        pass

    middleware = RequestAdmissionMiddleware(app, max_inflight=1)
    middleware.active = 1
    for s in (scope("/api/health"), scope("/api/health/loop"), scope("/"),
              scope("/api/test", "OPTIONS"), {"type": "websocket", "path": "/socket.io/"}):
        await middleware(s, receive, send)
    assert middleware.active == 1


@pytest.mark.parametrize("limit", [0, -1, 2001, True, 1.5])
def test_invalid_limit_fails_startup(limit):
    with pytest.raises(ValueError):
        RequestAdmissionMiddleware(None, max_inflight=limit)
