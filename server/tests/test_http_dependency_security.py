"""HTTP dependency regressions use only tiny synthetic requests and private files.

Exercise the security contracts behind the patched Starlette constraint rather
than asserting version strings. These tests do not boot the application/model
lifespan or contact any network service.
"""

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.requests import Request as StarletteRequest
from starlette.responses import FileResponse


@pytest.mark.parametrize(
    "host",
    [b"evil.invalid/another?x=", b"evil.invalid#fragment", b"evil.invalid@other.invalid"],
)
def test_malformed_host_cannot_replace_the_asgi_path_or_query(host):
    request = StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/private/records",
            "root_path": "",
            "query_string": b"real=1",
            "scheme": "https",
            "headers": [(b"host", host)],
            "server": ("orb.example.invalid", 443),
        }
    )
    assert request.url.path == "/api/private/records"
    assert request.url.query == "real=1"
    assert request.url.fragment == ""
    assert request.url.hostname == "orb.example.invalid"


def test_valid_host_preserves_the_actual_asgi_path():
    request = StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/private/records",
            "root_path": "",
            "query_string": b"real=1",
            "scheme": "https",
            "headers": [(b"host", b"ward.example.invalid:8443")],
            "server": ("orb.example.invalid", 443),
        }
    )
    assert request.url.path == "/api/private/records"
    assert request.url.hostname == "ward.example.invalid"
    assert request.url.port == 8443


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (b"a=1&b=2&c=3", 400, "Too many fields"),
        (b"a=12345678", 400, "Field exceeded maximum size"),
        (b"long_name=1", 400, "Field exceeded maximum size"),
        (b"a=1234567&b=2", 200, None),
        (b"", 200, None),
    ],
)
async def test_urlencoded_field_limits_apply_to_streamed_input(body, status, detail):
    app = FastAPI()
    parsed = []

    @app.post("/form")
    async def parse_form(request: Request):
        form = await request.form(max_fields=2, max_part_size=8)
        parsed.append(list(form.multi_items()))
        return {"fields": list(form.multi_items())}

    async def chunks():
        # A split inside a field exercises callback limits across ASGI frames.
        yield body[:4]
        yield body[4:]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid"
    ) as client:
        response = await client.post(
            "/form",
            content=chunks(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == status
    if detail is not None:
        assert detail in response.json()["detail"]
        assert parsed == [], "the handler must not treat rejected forms as parsed"
    else:
        expected = [["a", "1234567"], ["b", "2"]] if body else []
        assert response.json() == {"fields": expected}
        assert len(parsed) == 1


@pytest.mark.parametrize("suffix", ["4", "99999999999999999999999999999999999999"])
async def test_file_suffix_ranges_are_bounded_by_the_actual_file(tmp_path, suffix):
    path = tmp_path / "synthetic.txt"
    path.write_bytes(b"0123456789")
    app = FastAPI()

    @app.get("/file")
    async def get_file():
        return FileResponse(path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid"
    ) as client:
        response = await client.get("/file", headers={"range": f"bytes=-{suffix}"})
    expected = b"6789" if suffix == "4" else b"0123456789"
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["content-range"] == (
        "bytes 6-9/10" if suffix == "4" else "bytes 0-9/10"
    )
