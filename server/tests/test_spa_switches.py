"""The page carries the computer's switches (Orb Ward Record, 9 October 2026).

Samuel's ruling: fix the startup flash. The client assumed every clinical
switch was on until /api/health answered, so a Ward Record computer showed the
full rail and the score widgets for a moment after sign-in or a reload. The
server now writes window.__ORB_SWITCHES__ into index.html, before the app's
first module script, on every path that serves the page.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main

_DIST_INDEX = Path(__file__).resolve().parents[2] / "dist" / "index.html"
_needs_dist = pytest.mark.skipif(not _DIST_INDEX.exists(), reason="no built web app to serve")

_SCRIPT = re.compile(r"<script>window\.__ORB_SWITCHES__=(.*?);</script>", re.S)


def _switches_in(html: str) -> dict:
    found = _SCRIPT.findall(html)
    assert len(found) == 1, html[:2000]
    return json.loads(found[0])


@pytest.fixture
def ward_record(monkeypatch):
    monkeypatch.setattr(config, "WARD_RECORD", True)
    monkeypatch.setattr(config, "SCORING_ENABLED", False)
    monkeypatch.setattr(config, "LENS_ENABLED", False)
    monkeypatch.setattr(config, "SEPSIS_AUTOSTART", False)
    monkeypatch.setattr(config, "MODELS_ENABLED", False)


@pytest.fixture
def client():
    # The Host allow-list (services/host_gate.py) wraps socket_app; testserver
    # is not on it, so ask as localhost.
    return TestClient(main.socket_app, base_url="http://localhost")


@_needs_dist
@pytest.mark.parametrize("path", ["/", "/dashboard/vigil", "/index.html"])
def test_every_page_path_carries_the_profile_switches(ward_record, client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"
    switches = _switches_in(resp.text)
    assert switches["clinical"] == {
        "scoring_enabled": False, "lens_enabled": False, "sepsis_autostart": False,
        "models_enabled": False, "ward_record": True,
    }
    # The same maps /api/health publishes, so the probe and the page agree.
    health = client.get("/api/health").json()
    assert switches == {"clinical": health["clinical"], "modules": health["modules"],
                        "features": health["features"]}


@_needs_dist
def test_the_switches_run_before_the_first_module_script(client):
    html = client.get("/").text
    at_switches = html.find("window.__ORB_SWITCHES__")
    at_module = html.find('<script type="module"')
    assert at_switches != -1 and at_module != -1
    assert at_switches < at_module


@_needs_dist
def test_an_ordinary_site_says_ward_record_false(monkeypatch, client):
    monkeypatch.setattr(config, "WARD_RECORD", False)
    assert _switches_in(client.get("/dashboard").text)["clinical"]["ward_record"] is False


@_needs_dist
def test_a_revalidating_browser_still_gets_the_switches(client):
    # A browser holding an old copy must not be told 304 and keep a page that
    # carries switches from before a restart.
    first = client.get("/")
    etag = first.headers.get("etag")
    resp = client.get("/", headers={"If-None-Match": etag or '"x"',
                                    "If-Modified-Since": "Wed, 01 Jan 2099 00:00:00 GMT"})
    assert resp.status_code == 200
    _switches_in(resp.text)


@_needs_dist
def test_assets_and_api_404s_are_not_the_page(client):
    assert client.get("/assets/missing-file.js").status_code == 404
    resp = client.get("/api/no-such-route")
    assert resp.status_code == 404
    assert "__ORB_SWITCHES__" not in resp.text


def test_a_value_cannot_close_the_script_tag():
    hostile = {"clinical": {"ward_record": True},
               "modules": {"x</script><script>alert(1)</script>": True},
               "features": {"<!--": "&    "}}
    page = main.spa_index_with_switches(
        "<html><head><script type=\"module\" src=\"/a.js\"></script></head></html>", hostile)
    script = main.spa_switches_script(hostile)
    body = script[len("<script>"):-len("</script>")]
    for bad in ("</script", "<script", "<!--", "<", ">", "&", " ", " "):
        assert bad not in body, bad
    # Exactly the two scripts the page should have: ours, then the module one.
    assert page.count("</script>") == 2
    assert page.index(script) < page.index('<script type="module"')
    # And it still reads back as the same values in JavaScript's JSON grammar.
    assert json.loads(_SCRIPT.findall(page)[0]) == hostile


def test_a_page_with_no_script_gets_the_switches_in_its_head():
    page = main.spa_index_with_switches("<html><head><title>Orb</title></head></html>",
                                        {"clinical": {}, "modules": {}, "features": {}})
    assert page.index("__ORB_SWITCHES__") < page.index("</head>")
