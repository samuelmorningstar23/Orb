'use strict';

// Synthetic measurement regressions qualify the harness, never model latency.
const assert = require('node:assert/strict');
const { after, before, test } = require('node:test');
const http = require('node:http');
const { chromium } = require('@playwright/test');
const { SCENARIOS, loopbackOrigin, parseStream, assessTurn, installVisibleTimer, installWireObserver, prefixWarmEvidence, jsonGet } = require('./sage_browser_timing.cjs');
let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

const valid = () => ({
  request: { thinking: true, query: SCENARIOS[0].query }, httpStatus: 200, wireComplete: true,
  visibleAnswer: 'Oxygen saturation is the proportion of hemoglobin carrying oxygen.',
  browserTiming: { firstVisibleMs: 2999, completedVisibleMs: 15000 },
  stream: { done: true, errors: [], invalid: [], content: 'A real answer',
    metadata: { query_scope: 'GENERAL_MEDICAL', patient_context_used: false, provider: { model: 'medgemma:27b', downgraded: false } } },
});

test('SLA uses first readable answer while requiring successful completed output', () => {
  assert.equal(assessTurn(valid(), SCENARIOS[0]).targetMet, true);
  assert.equal(assessTurn(valid(), SCENARIOS[0]).completionWithinSameTarget, false);
  const threshold = valid();
  threshold.browserTiming.firstVisibleMs = 3000;
  assert.equal(assessTurn(threshold, SCENARIOS[0]).targetMet, false, 'strict less-than threshold');
});

test('errors, truncated streams, empty answers and silent model downgrades cannot pass', () => {
  for (const mutate of [
    turn => { turn.stream.done = false; },
    turn => { turn.wireComplete = false; },
    turn => { turn.stream.errors = [{ error: 'SAGE_STREAM_FAILED' }]; },
    turn => { turn.stream.content = ''; },
    turn => { turn.visibleAnswer = 'Connection failed. Ensure local models are running and responsive.'; },
    turn => { turn.stream.metadata.provider.model = 'medgemma:4b'; },
    turn => { turn.stream.metadata.provider.downgraded = true; },
    turn => { turn.request.thinking = false; },
    turn => { turn.request.query = 'unrelated question'; },
    turn => { turn.browserTiming.completedVisibleMs = null; },
    turn => { turn.stream.invalid = ['malformed']; },
    turn => { turn.httpStatus = 503; },
    turn => { turn.stream.metadata.query_scope = 'OUT_OF_SCOPE'; },
  ]) {
    const turn = valid();
    mutate(turn);
    assert.equal(assessTurn(turn, SCENARIOS[0]).targetMet, false);
  }
});

test('patient timing requires the verified chart and department timing requires BROAD scope', () => {
  const patient = { id: 'PAT001' };
  const turn = valid();
  turn.request.query = SCENARIOS[1].query;
  turn.stream.metadata = { query_scope: 'PATIENT_SPECIFIC', patient_context_used: true,
    patient_id: patient.id, provider: { model: 'medgemma:27b' } };
  assert.equal(assessTurn(turn, SCENARIOS[1], patient).targetMet, true);
  turn.stream.metadata.patient_id = 'PAT002';
  assert.equal(assessTurn(turn, SCENARIOS[1], patient).targetMet, false);
  turn.stream.metadata = { query_scope: 'AGGREGATE_STATS', provider: { model: 'medgemma:27b' } };
  turn.request.query = SCENARIOS[2].query;
  assert.equal(assessTurn(turn, SCENARIOS[2]).targetMet, false, 'a cheap count is not a department patient overview');
});

test('SSE parser preserves errors and requires explicit done across CRLF/multiline frames', () => {
  const stream = parseStream('data: {"content":"hello"}\r\n\r\ndata: {"metadata":\r\ndata: {"query_scope":"GENERAL_MEDICAL"}}\r\n\r\n'
    + 'data: {"error":"failed","code":"SAGE_STREAM_FAILED"}\n\ndata: {"done":true}\n\n');
  assert.equal(stream.content, 'hello');
  assert.equal(stream.metadata.query_scope, 'GENERAL_MEDICAL');
  assert.equal(stream.done, true);
  assert.equal(stream.errors.length, 1);
  assert.equal(parseStream('data: {"content":"partial"}\n\n').done, false);
});

test('only explicit loopback origins can be used for inference', () => {
  for (const url of ['https://127.0.0.1:11434', 'http://example.org:11434', 'http://127.0.0.2:11434',
    'http://u@127.0.0.1:11434', 'http://127.0.0.1:11434/api', 'http://127.0.0.1:11434?q=1']) {
    assert.throws(() => loopbackOrigin(url));
  }
  assert.equal(loopbackOrigin('http://127.0.0.1:11434/'), 'http://127.0.0.1:11434');
});

test('local metadata ignores inherited proxies, refuses external targets and bounds actual response size', async () => {
  const server = http.createServer((request, response) => {
    if (request.url === '/large') return response.end(' '.repeat(1_048_577));
    if (request.url === '/error') { response.writeHead(503); return response.end('{}'); }
    response.end(JSON.stringify({ synthetic: true }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const previousProxy = process.env.HTTP_PROXY;
  process.env.HTTP_PROXY = 'http://127.0.0.1:1';
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    assert.deepEqual(await jsonGet(`${origin}/api/tags`), { synthetic: true });
    await assert.rejects(jsonGet('http://example.org:11434/api/tags'), /loopback/);
    await assert.rejects(jsonGet(`http://credentials@127.0.0.1:${server.address().port}/api/tags`), /credentials/);
    await assert.rejects(jsonGet(`${origin}/error`), /HTTP 503/);
    await assert.rejects(jsonGet(`${origin}/large`), /1 MiB/);
  } finally {
    if (previousProxy === undefined) delete process.env.HTTP_PROXY;
    else process.env.HTTP_PROXY = previousProxy;
    await new Promise(resolve => server.close(resolve));
  }
});

test('chart prewarm evidence requires the actual model-prefix log for the exact patient', () => {
  const log = '2026-10-12 INFO [SagePreloader] PAT001 context compiled\n'
    + '2026-10-12 INFO [SagePrewarm] PAT002 prefix resident on medgemma:27b in 410 ms (prompt_eval=1080 tok, prompt_eval_ms=350)\n'
    + '2026-10-12 INFO [SagePrewarm] PAT001 prefix resident on medgemma:27b in 560 ms (prompt_eval=1094 tok, prompt_eval_ms=490)\n';
  assert.deepEqual(prefixWarmEvidence(log, 'PAT001'), {
    patientId: 'PAT001', model: 'medgemma:27b', durationMs: 560, promptTokens: 1094, promptEvaluationMs: 490,
  });
  assert.equal(prefixWarmEvidence(log, 'PAT003'), null);
  assert.equal(prefixWarmEvidence('[SagePreloader] PAT001 context compiled; cache hit', 'PAT001'), null,
    'a compiled facts cache is not proof of a resident model prefix');
  assert.equal(prefixWarmEvidence(log
    + '[SagePrewarm] PAT001 prefix resident on medgemma:4b in 70 ms (prompt_eval=20 tok, prompt_eval_ms=40)',
  'PAT001').model, 'medgemma:4b', 'retain the latest actual model instead of assuming the careful tier');
});

test('real DOM timer ignores old answers, loading stages, punctuation and hidden text', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<div class="sage-msg--assistant"><div class="sage-markdown">Old answer</div></div>'
      + '<div id="messages"></div><button class="sage-btn-send">Send</button>');
    await page.evaluate(installVisibleTimer);
    await page.evaluate(() => {
      document.querySelector('.sage-btn-send').addEventListener('click', () => {
        document.querySelector('button').className = 'sage-btn-stop';
        document.querySelector('#messages').innerHTML = '<div class="sage-stage-status">Generating answer</div>'
          + '<div class="sage-msg--assistant"><div class="sage-markdown">**</div></div>';
        setTimeout(() => {
          const answer = document.querySelector('#messages .sage-markdown');
          answer.style.visibility = 'hidden';
          answer.textContent = 'Readable synthetic answer';
        }, 100);
        setTimeout(() => { document.querySelector('#messages .sage-markdown').style.visibility = 'visible'; }, 220);
        setTimeout(() => { document.querySelector('button').className = 'sage-btn-send'; }, 380);
      });
    });
    await page.locator('button').click();
    await page.waitForFunction(() => Number.isFinite(window.__orbSageTiming.completedVisibleMs));
    const result = await page.evaluate(() => window.__orbSageTiming);
    assert.ok(result.firstVisibleMs >= 200, JSON.stringify(result));
    assert.ok(result.completedVisibleMs >= 360, JSON.stringify(result));
    assert.ok(result.completedVisibleMs > result.firstVisibleMs);
    assert.equal(result.streamingSeen, true);
  } finally { await page.close(); }
});

test('completion remains measurable after long details scroll a previously readable answer offscreen', async () => {
  const page = await browser.newPage({ viewport: { width: 900, height: 600 } });
  try {
    await page.setContent('<div id="messages"></div><button class="sage-btn-send" style="position:fixed;bottom:0">Send</button>');
    await page.evaluate(installVisibleTimer);
    await page.evaluate(() => {
      document.querySelector('button').addEventListener('click', () => {
        document.querySelector('button').className = 'sage-btn-stop';
        document.querySelector('#messages').innerHTML = '<div class="sage-msg--assistant">'
          + '<div class="sage-markdown">Readable synthetic answer</div></div>';
      });
    });
    await page.locator('button').click();
    await page.waitForFunction(() => Number.isFinite(window.__orbSageTiming.firstVisibleMs));
    await page.evaluate(() => {
      document.querySelector('.sage-msg--assistant').insertAdjacentHTML('beforeend', '<div style="height:1600px">Long structured details</div>');
      window.scrollTo(0, 1200);
      document.querySelector('.sage-markdown').style.visibility = 'hidden';
      document.querySelector('button').className = 'sage-btn-send';
    });
    await page.waitForTimeout(50);
    assert.equal(await page.evaluate(() => window.__orbSageTiming.completedVisibleMs), null,
      'a hidden final answer is not rendered completion');
    await page.evaluate(() => { document.querySelector('.sage-markdown').style.visibility = 'visible'; });
    await page.waitForFunction(() => Number.isFinite(window.__orbSageTiming.completedVisibleMs));
    const result = await page.evaluate(() => ({ ...window.__orbSageTiming,
      answerBottom: document.querySelector('.sage-markdown').getBoundingClientRect().bottom }));
    assert.ok(result.answerBottom < 0, 'the answer remains outside the viewport');
    assert.ok(result.completedVisibleMs >= result.firstVisibleMs + 50);
    assert.equal(result.streamingSeen, true);
  } finally { await page.close(); }
});

test('native fetch observation preserves terminal SSE errors without altering the application response', async () => {
  const server = http.createServer((request, response) => {
    if (request.method === 'POST' && request.url === '/api/sage/query') {
      response.writeHead(200, { 'Content-Type': 'text/event-stream' });
      response.write('data: {"content":"partial"}\n\n');
      setTimeout(() => response.end('data: {"error":"failed","code":"SAGE_STREAM_FAILED"}\n\n'), 30);
    } else {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end('<html><body>Synthetic wire-observer regression only</body></html>');
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const page = await browser.newPage();
  try {
    await page.addInitScript(installWireObserver);
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const actual = await page.evaluate(async () => (await fetch('/api/sage/query', { method: 'POST', body: '{}' })).text());
    await page.waitForFunction(() => window.__orbSageWireTurns[0]?.eof === true);
    const observed = await page.evaluate(() => window.__orbSageWireTurns[0]);
    assert.equal(observed.raw, actual);
    assert.equal(observed.status, 200);
    const parsed = parseStream(observed.raw);
    assert.equal(parsed.done, false);
    assert.equal(parsed.errors[0].code, 'SAGE_STREAM_FAILED');
  } finally {
    await page.close();
    await new Promise(resolve => server.close(resolve));
  }
});
