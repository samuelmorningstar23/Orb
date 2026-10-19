"""Extracted infrastructure contracts without importing Orb or opening a DB."""
import asyncio
import json

import pytest

from security_headers import SecurityHeadersMiddleware
from services.warm_policy import keep_warm_backoff_ticks, select_models_to_warm, warm_budget_seconds
from spa_bootstrap import spa_index_with_switches, spa_switches_script


@pytest.mark.parametrize('scope,secure', [
    ({'type':'http','scheme':'http'}, False),
    ({'type':'http','scheme':'https'}, True),
    ({'type':'http','headers':[(b'x-forwarded-proto',b' HTTPS ,http')]}, True),
    ({'type':'http','headers':[(b'x-forwarded-proto',b'http,https')]}, False),
])
def test_streaming_security_headers_preserve_bodies_and_route_values(scope, secure):
    sent = []
    headers = [(b'X-Frame-Options',b'DENY')]
    original = {'type':'http.response.start','status':200,'headers':headers}
    async def receive():
        return {'type':'http.request'}
    async def send(message):
        sent.append(message)
    async def app(scope, receive, send):
        await send(original)
        await send({'type':'http.response.body','body':b'first','more_body':True})
        await send({'type':'http.response.body','body':b'last'})
    asyncio.run(SecurityHeadersMiddleware(app)(scope,receive,send))
    actual = {key.lower():value for key,value in sent[0]['headers']}
    assert actual[b'x-frame-options'] == b'DENY'
    assert actual[b'x-content-type-options'] == b'nosniff'
    assert actual[b'referrer-policy'] == b'no-referrer'
    assert actual[b'content-security-policy'].startswith(b"frame-ancestors 'self'")
    assert (b'strict-transport-security' in actual) is secure
    assert [x['body'] for x in sent[1:]] == [b'first',b'last']
    assert original['headers'] is headers and headers == [(b'X-Frame-Options',b'DENY')]


def test_non_http_security_messages_pass_through():
    sent=[]
    async def receive(): return {'type':'websocket.connect'}
    async def send(message): sent.append(message)
    async def app(scope,receive,send): await send({'type':'websocket.accept'})
    asyncio.run(SecurityHeadersMiddleware(app)({'type':'websocket'},receive,send))
    assert sent == [{'type':'websocket.accept'}]


@pytest.mark.parametrize('html', ['<head><script type="module"></script></head>', '<head></head>', 'plain'])
def test_bootstrap_is_safe_and_precedes_the_first_script(html):
    switches = {'feature':'</script><!-- & > नमस्ते\u2028\u2029'}
    script = spa_switches_script(switches)
    assert script.count('</script>') == 1 and '<!--' not in script
    assert '\\u003c' in script and '\\u0026' in script
    payload = script.removeprefix('<script>window.__ORB_SWITCHES__=').removesuffix(';</script>')
    assert json.loads(payload) == switches
    page = spa_index_with_switches(html, switches)
    assert page.replace(script,'',1) == html
    if '<script' in html: assert page.index(script) < page.index('<script type="module"')
    elif '</head>' in html: assert page.index(script) < page.index('</head>')
    else: assert page.startswith(script)


@pytest.mark.parametrize('raw,expected', [(None,45),('20',20),('0',5),('-1',5),('bad',45),('nan',45),('inf',45),('-inf',45)])
def test_warm_timeout_is_finite_and_keeps_the_established_policy(monkeypatch,raw,expected):
    if raw is None: monkeypatch.delenv('ORB_KEEP_WARM_BUDGET',raising=False)
    else: monkeypatch.setenv('ORB_KEEP_WARM_BUDGET',raw)
    assert warm_budget_seconds() == expected


def test_backoff_saturates_without_computing_a_huge_integer():
    assert [keep_warm_backoff_ticks(x) for x in (-1,0,1,2,3,4,5,1000000000)] == [0,0,1,2,4,8,12,12]


def test_model_selection_retains_order_and_operator_holds():
    assert select_models_to_warm(['a','b','a','c','d'],{'b'},{'c'}) == ['a','d']
    assert select_models_to_warm([],set(),set()) == []
