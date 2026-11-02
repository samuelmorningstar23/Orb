#!/usr/bin/env node
'use strict';

// Real, opt-in local-inference qualification. The script owns a disposable
// seeded backend; it cannot attach to an existing appliance or fulfill an API.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');
const { finished } = require('node:stream/promises');

const ROOT = path.resolve(__dirname, '..');
const MODEL = 'medgemma:27b';
const TARGETS_MS = Object.freeze({ general: 3000, patient: 5000, department: 10000 });
const SCENARIOS = Object.freeze([
  { category: 'general', query: 'What does oxygen saturation measure? Answer briefly.', scopes: ['GENERAL_MEDICAL'] },
  { category: 'patient', query: 'Summarize Ananya Kapoor\'s current chart and the recorded facts that need clinician review. Answer briefly.',
    scopes: ['PATIENT_SPECIFIC', 'PATIENT_ANCHORED'], patientName: 'Ananya Kapoor' },
  { category: 'department', query: 'Give a concise overview of my department\'s admitted patients and identify recorded concerns for clinician review.', scopes: ['BROAD'] },
]);

function loopbackOrigin(value) {
  const url = new URL(value);
  assert.equal(url.protocol, 'http:', 'only loopback HTTP is permitted');
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname), 'only loopback HTTP is permitted');
  assert.ok(url.port && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/',
    'a loopback origin with an explicit port is required');
  return url.origin;
}

function parseStream(text) {
  const events = [];
  const invalid = [];
  for (const block of text.replace(/\r\n/g, '\n').split(/\n\n+/)) {
    const data = block.split('\n').filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).replace(/^ /, '')).join('\n');
    if (!data) continue;
    try { events.push(JSON.parse(data)); }
    catch { invalid.push(data.slice(0, 80)); }
  }
  return {
    done: events.some(event => event?.done === true),
    errors: events.filter(event => event?.error).map(event => ({ error: event.error, code: event.code })),
    content: events.filter(event => typeof event?.content === 'string').map(event => event.content).join(''),
    metadata: Object.assign({}, ...events.filter(event => event?.metadata).map(event => event.metadata)),
    structured: events.findLast(event => event?.structured_response)?.structured_response ?? null,
    stages: events.filter(event => event?.stage).map(event => event.stage),
    invalid,
  };
}

function assessTurn(turn, scenario, patient) {
  const failures = [];
  const stream = turn.stream;
  if (turn.error) failures.push(turn.error);
  if (turn.httpStatus !== 200) failures.push(`HTTP ${turn.httpStatus ?? 'not observed'}`);
  if (turn.wireComplete !== true) failures.push('actual response stream did not reach EOF');
  if (!stream?.done) failures.push('explicit successful Sage done frame missing');
  if (stream?.errors?.length) failures.push('Sage reported a stream error');
  if (stream?.invalid?.length) failures.push('invalid SSE JSON framing');
  if (!stream?.content?.trim()) failures.push('nonempty model answer missing');
  if (!turn.visibleAnswer?.trim() || /Connection failed|Ensure local models|generation (?:was )?stopped/i.test(turn.visibleAnswer)) {
    failures.push('successful visible answer missing');
  }
  const timing = turn.browserTiming || {};
  if (!(Number.isFinite(timing.firstVisibleMs) && timing.firstVisibleMs >= 0)) failures.push('first visible answer time missing');
  if (!(Number.isFinite(timing.completedVisibleMs) && timing.completedVisibleMs >= timing.firstVisibleMs)) {
    failures.push('completed visible answer time missing');
  }
  if (turn.request?.thinking !== true) failures.push('thinking mode was not on');
  if (turn.request?.query !== scenario.query) failures.push('observed request did not contain the submitted question');
  if (!scenario.scopes.includes(stream?.metadata?.query_scope)) failures.push('actual query scope differs from scenario');
  const provider = stream?.metadata?.provider;
  if (provider?.model !== MODEL || provider?.downgraded === true) failures.push('careful27b provenance missing or downgraded');
  if (scenario.category === 'patient') {
    if (!patient || stream?.metadata?.patient_context_used !== true || stream?.metadata?.patient_id !== patient.id) {
      failures.push('the verified synthetic patient chart was not used');
    }
  } else if (stream?.metadata?.patient_context_used === true || turn.request?.patient_id) {
    failures.push('unexpected individual patient anchoring');
  }
  const success = failures.length === 0;
  const targetMs = TARGETS_MS[scenario.category];
  return { success, failures, targetMs,
    targetMet: success && timing.firstVisibleMs < targetMs,
    completionWithinSameTarget: success && timing.completedVisibleMs < targetMs };
}

// Executed in the real page. A capturing click listener starts the clock before
// React handles submit. Text must be painted inside the newest answer bubble;
// stage/progress text and earlier conversations cannot satisfy it. Completion
// means the real UI has left streaming, validated separately against SSE done.
function installVisibleTimer() {
  const previous = window.__orbSageTimingCleanup;
  if (previous) previous();
  const initial = document.querySelectorAll('.sage-msg--assistant').length;
  const state = { submittedAt: null, firstVisibleMs: null, completedVisibleMs: null, streamingSeen: false };
  window.__orbSageTiming = state;
  let scheduled = false;
  let disposed = false;
  const rendered = element => {
    const rect = element.getBoundingClientRect();
    if (!(rect.width > 0 && rect.height > 0)) return false;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const sample = () => {
    scheduled = false;
    if (disposed || state.submittedAt === null || state.completedVisibleMs !== null) return;
    const stop = document.querySelector('.sage-btn-stop');
    if (stop) state.streamingSeen = true;
    const assistants = document.querySelectorAll('.sage-msg--assistant');
    const answer = assistants.length > initial ? assistants[assistants.length - 1].querySelector('.sage-markdown') : null;
    // Require a readable fragment, not formatting punctuation or one unfinished
    // token. This definition is recorded with the results for reproducibility.
    const text = answer?.textContent.trim() || '';
    if (answer && !/^Connection failed\./.test(text) && text.length >= 12
      && (text.match(/[\p{L}\p{N}]{2,}/gu) || []).length >= 2 && rendered(answer)) {
      const elapsed = performance.now() - state.submittedAt;
      const rect = answer.getBoundingClientRect();
      const inViewport = rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth;
      if (state.firstVisibleMs === null && inViewport) state.firstVisibleMs = elapsed;
      // Expanded structured details may scroll a previously readable answer
      // above the viewport. Completion is the rendered answer + finished UI,
      // validated against actual stream EOF/done; it does not require the
      // whole long answer to fit on screen again.
      if (state.firstVisibleMs !== null && !stop && document.querySelector('.sage-btn-send')) {
        state.completedVisibleMs = elapsed;
      }
    }
    if (state.completedVisibleMs === null) schedule();
  };
  const schedule = () => {
    if (!scheduled && !disposed) { scheduled = true; requestAnimationFrame(sample); }
  };
  const click = event => {
    if (state.submittedAt === null && event.target.closest('.sage-btn-send')) {
      state.submittedAt = performance.now();
      schedule();
    }
  };
  const observer = new MutationObserver(schedule);
  observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true });
  document.addEventListener('click', click, true);
  window.__orbSageTimingCleanup = () => {
    disposed = true;
    observer.disconnect();
    document.removeEventListener('click', click, true);
  };
}

function installWireObserver() {
  window.__orbSageWireTurns = [];
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const input = args[0];
    const init = args[1];
    const url = new URL(typeof input === 'string' || input instanceof URL ? String(input) : input.url, location.href);
    if (url.pathname !== '/api/sage/query' || (init?.method || input.method || 'GET').toUpperCase() !== 'POST') {
      return nativeFetch(...args);
    }
    const record = { raw: '', eof: false, status: null, error: null };
    window.__orbSageWireTurns.push(record);
    try {
      const response = await nativeFetch(...args);
      record.status = response.status;
      // Observe actual bytes in a separate native Response clone. The response
      // handed to the app is unchanged; no route fulfillment or API mock exists.
      response.clone().text().then(text => { record.raw = text; record.eof = true; })
        .catch(error => { record.error = error.message; record.eof = true; });
      return response;
    } catch (error) {
      record.error = error.message;
      record.eof = true;
      throw error;
    }
  };
}

async function jsonGet(url) {
  const address = new URL(url);
  loopbackOrigin(address.origin);
  assert.ok(!address.username && !address.password, 'local metadata credentials are not permitted');
  // Native HTTP makes no environment-proxy selection. Model metadata must not
  // leave loopback even if the shell opts Node's fetch into HTTP_PROXY support.
  return new Promise((resolve, reject) => {
    const request = http.get(address, response => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`local metadata request failed: HTTP ${response.statusCode}`));
        return;
      }
      const chunks = [];
      let bytes = 0;
      response.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 1_048_576) request.destroy(new Error('local metadata exceeded the 1 MiB limit'));
        else chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('end', () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
        catch (error) { reject(error); }
      });
    });
    request.setTimeout(5000, () => request.destroy(new Error('local metadata request timed out')));
    request.on('error', reject);
  });
}

async function unusedPort(port) {
  const probe = net.createServer();
  await new Promise((resolve, reject) => {
    probe.once('error', reject);
    probe.listen(port, '127.0.0.1', resolve);
  });
  await new Promise(resolve => probe.close(resolve));
}

async function startBackend(python, port, inference, output, snapshots, journeys, chartPrewarm, signal) {
  signal.throwIfAborted();
  await unusedPort(port); // Never reuse or authenticate against an existing app.
  const log = fs.createWriteStream(path.join(output, 'backend.log'), { flags: 'wx' });
  const command = [path.join(ROOT, 'scripts/e2e_server.py'), '--host', '127.0.0.1',
    '--port', String(port), '--local-inference-url', inference];
  if (snapshots.embedding) command.push('--embedding-model', snapshots.embedding, '--reranker-model', snapshots.reranker);
  if (journeys) command.push('--offline-modules');
  if (chartPrewarm) command.push('--chart-prewarm');
  const child = spawn(python, command, { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  let text = '';
  let exit = null;
  const exited = new Promise(resolve => child.once('exit', (code, signal) => { exit = { code, signal }; resolve(exit); }));
  const childError = new Promise(resolve => child.once('error', error => { exit = { error: error.message }; resolve(exit); }));
  const consume = chunk => {
    log.write(chunk);
    text = (text + chunk.toString()).slice(-100000);
  };
  child.stdout.on('data', consume);
  child.stderr.on('data', consume);
  let stopping;
  const stop = () => stopping ||= (async () => {
    if (!exit) child.kill('SIGTERM');
    let timer;
    const expired = await Promise.race([exited.then(() => false), childError.then(() => false),
      new Promise(resolve => { timer = setTimeout(() => resolve(true), 25000); })]);
    clearTimeout(timer);
    signal.removeEventListener('abort', abort);
    // The Python launcher owns process-group cleanup. Killing only its leader
    // could orphan a DB writer; leave a clear failure instead of doing that.
    log.end();
    if (expired) throw new Error('owned backend did not finish graceful process-group cleanup');
  })();
  const abort = () => { void stop().catch(() => {}); };
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) abort();
  try {
    const deadline = Date.now() + 600000;
    const base = `http://127.0.0.1:${port}`;
    while (!text.includes(`Uvicorn running on ${base}`)) {
      if (exit) throw new Error(`owned synthetic backend exited before listening: ${JSON.stringify(exit)}`);
      if (Date.now() > deadline) throw new Error('owned synthetic backend startup timed out');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal((await jsonGet(`${base}/api/ready`)).status, 'ready');
    const line = text.split('\n').find(row => row.startsWith('E2E local inference: '));
    assert.ok(line, 'isolated inference launcher metadata missing');
    const corpus = text.split('\n').find(row => row.startsWith('E2E knowledge corpus: '));
    return { child, stop, base, configuration: JSON.parse(line.slice('E2E local inference: '.length)),
      corpus: corpus ? JSON.parse(corpus.slice('E2E knowledge corpus: '.length)) : null };
  } catch (error) {
    await stop();
    throw error;
  }
}

async function login(page, base) {
  await page.goto(`${base}/login/staff`);
  await page.locator('#input-employee-id').fill('DOC001');
  await page.locator('#input-password').fill('password');
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/auth/login' && r.request().method() === 'POST');
  await page.locator('#btn-login').click();
  assert.equal((await response).status(), 200, 'synthetic doctor login failed');
  await page.waitForURL('**/dashboard**');
  const userResponse = await page.request.get(`${base}/api/auth/me`);
  assert.equal(userResponse.status(), 200);
  const user = await userResponse.json();
  assert.equal(user.id, 'DOC001');
  assert.equal(user.department, 'General Medicine');
  await page.locator('#nav-sage').click();
  await page.locator('.sage-composer__input').waitFor({ state: 'visible' });
  assert.equal(await page.getByRole('switch', { name: 'Thinking mode' }).getAttribute('aria-checked'), 'true');
  return { id: user.id, role: user.role, department: user.department };
}

async function runTurn(page, inference, scenario, patient, output, index, timeoutMs) {
  const turn = { index, category: scenario.category, query: scenario.query, startedAt: new Date().toISOString(),
    residencyBefore: await jsonGet(`${inference}/api/ps`) };
  turn.residency = turn.residencyBefore.models?.some(model => model.name === MODEL) ? 'resident/warm runner' : 'not resident/cold runner';
  await page.getByRole('button', { name: 'New Chat', exact: true }).click();
  await page.locator('.sage-msg--assistant').waitFor({ state: 'hidden' });
  await page.locator('.sage-composer__input').fill(scenario.query);
  await page.evaluate(installVisibleTimer);
  const wireIndex = await page.evaluate(() => window.__orbSageWireTurns.length);
  const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname === '/api/sage/query'
    && response.request().method() === 'POST', { timeout: timeoutMs });
  responsePromise.catch(() => {}); // The click itself can fail before the awaited response.
  let deadlineTimer;
  try {
    await page.locator('.sage-btn-send').click();
    const response = await responsePromise;
    turn.request = response.request().postDataJSON();
    turn.httpStatus = response.status();
    // A cloned native fetch stream survives browser/CDP body-buffer eviction,
    // including failed SSE responses. Completion of an error UI is recorded as
    // failure by assessTurn, never accepted as answer latency.
    await Promise.race([page.waitForFunction(index => window.__orbSageWireTurns[index]?.eof
      && !document.querySelector('.sage-btn-stop'), wireIndex, { timeout: timeoutMs }), new Promise((_, reject) => {
      deadlineTimer = setTimeout(() => reject(new Error('real answer completion timed out')), timeoutMs);
    })]);
    await page.waitForTimeout(40); // Allow the completion DOM frame to be sampled.
  } catch (error) {
    turn.error = error.message;
  } finally {
    clearTimeout(deadlineTimer);
    const wire = await page.evaluate(index => window.__orbSageWireTurns[index], wireIndex);
    if (wire) {
      turn.stream = parseStream(wire.raw);
      turn.wireComplete = wire.eof;
      if (wire.error) turn.error = `actual fetch stream failed: ${wire.error}`;
    }
    turn.browserTiming = await page.evaluate(() => window.__orbSageTiming);
    turn.visibleAnswer = await page.locator('.sage-msg--assistant .sage-markdown').last().innerText().catch(() => '');
    await page.evaluate(async () => {
      await Promise.all(document.getAnimations().filter(animation => Number.isFinite(animation.effect?.getTiming().iterations))
        .map(animation => animation.finished.catch(() => {})));
      document.querySelectorAll('.sage-msg--assistant .sage-markdown').item(0)?.scrollIntoView({ block: 'start', behavior: 'instant' });
    });
    const filename = `${String(index).padStart(2, '0')}-${scenario.category}.png`;
    await page.screenshot({ path: path.join(output, filename), fullPage: true });
    turn.screenshot = filename;
    turn.assessment = assessTurn(turn, scenario, patient);
    turn.residencyAfter = await jsonGet(`${inference}/api/ps`);
    await page.evaluate(() => window.__orbSageTimingCleanup?.());
    // An unsuccessful timed-out turn cannot occupy the next turn's lane.
    if (await page.locator('.sage-btn-stop').count()) await page.locator('.sage-btn-stop').click();
    console.log(JSON.stringify({ category: turn.category, residency: turn.residency,
      firstVisibleMs: turn.browserTiming.firstVisibleMs, completedVisibleMs: turn.browserTiming.completedVisibleMs,
      ...turn.assessment }));
  }
  return turn;
}

function prefixWarmEvidence(log, patientId) {
  const rows = [...log.matchAll(/\[SagePrewarm\] (\S+) prefix resident on (\S+) in ([\d.]+) ms \(prompt_eval=(\d+) tok, prompt_eval_ms=([\d.]+)\)/g)];
  const row = rows.findLast(match => match[1] === patientId);
  return row ? { patientId: row[1], model: row[2], durationMs: Number(row[3]),
    promptTokens: Number(row[4]), promptEvaluationMs: Number(row[5]) } : null;
}

async function runChartPrewarm(page, base, inference, patient, output, timeoutMs) {
  await page.locator('#nav-vigil').click();
  const row = page.locator('.patient-row').filter({ has: page.locator('.patient-row__name', { hasText: patient.name }) });
  await row.waitFor({ state: 'visible' });
  assert.equal(await row.count(), 1, 'the verified synthetic patient must have one visible chart row');
  await page.evaluate(() => {
    window.__orbChartOpenedAt = null;
    const click = event => {
      if (event.target.closest('.patient-row')) {
        window.__orbChartOpenedAt = performance.now();
        document.removeEventListener('click', click, true);
      }
    };
    document.addEventListener('click', click, true);
  });
  const prefetch = page.waitForResponse(response => new URL(response.url()).pathname === '/api/sage/prefetch'
    && response.request().method() === 'POST' && response.request().postDataJSON()?.patient_id === patient.id,
  { timeout: timeoutMs });
  prefetch.catch(() => {});
  await row.click();
  const response = await prefetch;
  assert.equal(response.status(), 200, 'real authorized chart prefetch failed');
  const body = await response.json();
  assert.equal(body.status, 'warmed', 'the chart facts cache was not warmed');
  assert.equal(body.patient_id, patient.id);
  await page.locator('.command-bar__name').filter({ hasText: patient.name }).waitFor({ state: 'visible' });
  const times = await page.evaluate(() => ({ openedAt: window.__orbChartOpenedAt, prefetchFinishedAt: performance.now() }));
  await page.screenshot({ path: path.join(output, 'prewarmed-patient-chart.png'), fullPage: true });
  await page.locator('#nav-sage').click();
  const turn = await runTurn(page, inference, SCENARIOS[1], patient, output, 'chart-prewarm', timeoutMs);
  const prefix = prefixWarmEvidence(fs.readFileSync(path.join(output, 'backend.log'), 'utf8'), patient.id);
  return { ...turn, phase: 'additional real chart-open prefix prewarm; baseline observations remain separate',
    chart: { patientId: patient.id, prefetchResponse: body, prefetchDurationMs: times.prefetchFinishedAt - times.openedAt,
      openToSubmitMs: turn.browserTiming.submittedAt - times.openedAt,
      openToFirstReadableMs: turn.browserTiming.firstVisibleMs === null ? null
        : turn.browserTiming.submittedAt - times.openedAt + turn.browserTiming.firstVisibleMs,
      prefixWarmEvidence: prefix, prefixConfirmed: prefix?.model === MODEL,
      screenshot: 'prewarmed-patient-chart.png' } };
}

async function runJourneys(base, output, signal) {
  signal.throwIfAborted();
  const stdout = fs.createWriteStream(path.join(output, 'journeys.json'), { flags: 'wx' });
  const stderr = fs.createWriteStream(path.join(output, 'journeys.log'), { flags: 'wx' });
  const child = spawn(process.execPath, [path.join(ROOT, 'node_modules/@playwright/test/cli.js'), 'test',
    '--reporter=json', `--output=${path.join(output, 'journey-artifacts')}`], {
    cwd: ROOT, env: { ...process.env, ORB_E2E_NO_WEBSERVER: '1', ORB_E2E_BASE_URL: base },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const streamsFinished = Promise.all([finished(stdout), finished(stderr)]);
  streamsFinished.catch(() => {});
  child.stdout.pipe(stdout);
  child.stderr.pipe(stderr);
  const abort = () => child.kill('SIGTERM');
  signal.addEventListener('abort', abort, { once: true });
  const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); });
  signal.removeEventListener('abort', abort);
  await streamsFinished;
  const report = JSON.parse(fs.readFileSync(path.join(output, 'journeys.json'), 'utf8'));
  return { exitCode: code, stats: report.stats, report: 'journeys.json', artifacts: 'journey-artifacts' };
}

async function main() {
  const args = Object.fromEntries(process.argv.slice(2).map(arg => {
    const split = arg.indexOf('=');
    assert.ok(split > 2, 'use --option=value arguments; see scripts/sage_browser_timing.md');
    return [arg.slice(2, split), arg.slice(split + 1)];
  }));
  const options = new Set(['python', 'port', 'warm-runs', 'timeout-ms', 'inference-url', 'embedding-model', 'reranker-model', 'out', 'headed', 'journeys', 'chart-prewarm', 'chart-prewarm-only']);
  for (const key of Object.keys(args)) assert.ok(options.has(key), `unknown option --${key}`);
  for (const key of ['headed', 'journeys', 'chart-prewarm', 'chart-prewarm-only']) {
    if (key in args) assert.ok(['true', 'false'].includes(args[key]), `--${key} must be true or false`);
  }
  assert.ok(args.python, '--python=/absolute/path/to/owned/runtime/bin/python is required');
  assert.ok(path.isAbsolute(args.python) && fs.existsSync(args.python), 'the Python runtime must exist at an absolute path');
  const port = Number(args.port || 18120);
  const repeats = Number(args['warm-runs'] || 1);
  const timeoutMs = Number(args['timeout-ms'] || 180000);
  assert.ok(Number.isInteger(port) && port >= 1024 && port <= 65535, 'invalid port');
  assert.ok(Number.isInteger(repeats) && repeats >= 1 && repeats <= 5, 'warm-runs must be 1..5');
  assert.ok(Number.isInteger(timeoutMs) && timeoutMs >= 10000 && timeoutMs <= 600000, 'invalid timeout-ms');
  const inference = loopbackOrigin(args['inference-url'] || 'http://127.0.0.1:11434');
  const snapshots = { embedding: args['embedding-model'], reranker: args['reranker-model'] };
  assert.equal(Boolean(snapshots.embedding), Boolean(snapshots.reranker), 'provide both installed snapshots or neither');
  assert.ok(args['chart-prewarm-only'] !== 'true' || args['chart-prewarm'] === 'true', 'chart-prewarm-only requires chart-prewarm=true');
  const output = args.out ? path.resolve(args.out) : fs.mkdtempSync(path.join(os.tmpdir(), 'orb-sage-browser-'));
  if (args.out) fs.mkdirSync(output, { recursive: false }); // Refuse to overwrite evidence.
  const report = { startedAt: new Date().toISOString(), output, qualification: 'idle synthetic local inference; not busy-ward acceptance',
    baselineRequested: args['chart-prewarm-only'] !== 'true',
    normalMaintenance: 'ordinary non-model maintenance remains enabled; overlap is retained in backend.log',
    targets: { interpretation: 'strict submit-to-first-readable-visible-answer; successful completion required', milliseconds: TARGETS_MS,
      readableDefinition: 'newest visible answer bubble; at least12characters and2letter-or-number words; sampled with requestAnimationFrame',
      coldStartPolicy: 'initial observed residency diagnostic recorded separately; SLA applies to subsequent resident-model turns' },
    python: execFileSync(args.python, ['-c', 'import sys; print(sys.version)'], { encoding: 'utf8' }).trim(),
    hardware: { platform: os.platform(), architecture: os.arch(), cpu: os.cpus()[0]?.model, logicalCpus: os.cpus().length, memoryBytes: os.totalmem() },
    bundleSha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, 'dist/index.html'))).digest('hex'),
    harnessSha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),
    backendSourceSha256: Object.fromEntries(['routes/sage_routes.py', 'services/sage_service.py', 'services/llm_providers.py',
      'services/clinical_knowledge.py', 'services/local_transformers.py'].map(file => [file,
      crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT, 'server', file))).digest('hex')])),
    modelService: { origin: inference, tags: await jsonGet(`${inference}/api/tags`),
      version: await jsonGet(`${inference}/api/version`), residencyAtStart: await jsonGet(`${inference}/api/ps`) },
    retrieval: snapshots.embedding ? 'explicit installed embedder/reranker snapshots; real corpus initialized in disposable DB; isolated worker'
      : 'temporary empty offline HF cache; no appliance knowledge/model cache copied; retrieval may degrade', turns: [],
    blockedExternalRequests: [], pageErrors: [], consoleErrors: [] };
  let backend;
  let browser;
  let page;
  let cleanupFailure;
  const cancellation = new AbortController();
  const interrupted = () => {
    report.error = 'timing run interrupted';
    report.interruptedAt = new Date().toISOString();
    cancellation.abort();
    // Close the page to release any wait, then let finally stop the launcher.
    // During startup the Python launcher must receive TERM to clean its group.
    if (browser) void browser.close().catch(() => {});
    if (backend) void backend.stop().catch(error => { cleanupFailure = error.message; });
  };
  process.once('SIGINT', interrupted);
  process.once('SIGTERM', interrupted);
  try {
    backend = await startBackend(args.python, port, inference, output, snapshots, args.journeys === 'true', args['chart-prewarm'] === 'true', cancellation.signal);
    report.backend = backend.configuration;
    report.knowledgeCorpus = backend.corpus;
    if (snapshots.embedding) assert.ok(backend.corpus?.sources > 0 && backend.corpus.vectors === backend.corpus.sources);
    report.base = backend.base;
    const { chromium } = require('@playwright/test');
    browser = await chromium.launch({ headless: args.headed !== 'true' });
    report.browserVersion = browser.version();
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' });
    await context.addInitScript(installWireObserver);
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (['http:', 'https:'].includes(url.protocol) && url.origin !== backend.base) {
        report.blockedExternalRequests.push({ origin: url.origin, path: url.pathname });
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    page = await context.newPage();
    page.setDefaultTimeout(30000);
    page.on('pageerror', error => report.pageErrors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
    page.on('websocket', socket => {
      const url = new URL(socket.url());
      if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
        report.blockedExternalRequests.push({ origin: url.origin, path: url.pathname, kind: 'unexpected websocket' });
        void page.close();
      }
    });
    report.actor = await login(page, backend.base);
    const patientsResponse = await page.request.get(`${backend.base}/api/vigil/patients`);
    assert.equal(patientsResponse.status(), 200);
    const patients = await patientsResponse.json();
    const patient = patients.find(row => row.name === 'Ananya Kapoor');
    assert.ok(patient?.id && patient.department === 'General Medicine', 'the synthetic patient must be in the clinician department');
    report.syntheticPatient = { id: patient.id, name: patient.name, department: patient.department };
    const posture = await page.request.get(`${backend.base}/api/sage/ai-posture`);
    assert.equal(posture.status(), 200);
    report.aiPosture = await posture.json();
    assert.equal(report.aiPosture.local_model, MODEL);
    // First observed state is retained; this script never unloads a shared model
    // to fabricate a cold run. Each following turn records its own residency.
    if (args['chart-prewarm-only'] !== 'true') {
      const initial = await runTurn(page, inference, SCENARIOS[0], patient, output, 0, timeoutMs);
      initial.phase = 'initial residency diagnostic; excluded from warm SLA';
      report.turns.push(initial);
      fs.writeFileSync(path.join(output, 'timings.current.json'), JSON.stringify(report, null, 2) + '\n');
      for (let repeat = 0; repeat < repeats; repeat++) {
        for (const scenario of SCENARIOS) {
          const turn = await runTurn(page, inference, scenario, patient, output, report.turns.length, timeoutMs);
          turn.repeat = repeat + 1;
          turn.phase = 'warm SLA';
          report.turns.push(turn);
          fs.writeFileSync(path.join(output, 'timings.current.json'), JSON.stringify(report, null, 2) + '\n');
        }
      }
    }
    if (args['chart-prewarm'] === 'true') {
      report.chartPrewarm = await runChartPrewarm(page, backend.base, inference, patient, output, timeoutMs);
      fs.writeFileSync(path.join(output, 'timings.current.json'), JSON.stringify(report, null, 2) + '\n');
    }
    if (args.journeys === 'true') {
      await browser.close();
      browser = null;
      report.journeys = await runJourneys(backend.base, output, cancellation.signal);
    }
  } catch (error) {
    report.error ||= error.message;
    if (page && !page.isClosed()) await page.screenshot({ path: path.join(output, 'run-failure.png'), fullPage: true }).catch(() => {});
    console.error(error.stack);
  } finally {
    if (browser) await browser.close().catch(error => { cleanupFailure = error.message; });
    if (backend) await backend.stop().catch(error => { cleanupFailure = error.message; });
    process.removeListener('SIGINT', interrupted);
    process.removeListener('SIGTERM', interrupted);
    report.finishedAt = new Date().toISOString();
    if (cleanupFailure) report.cleanupFailure = cleanupFailure;
    report.passed = !report.error && !cleanupFailure && !report.blockedExternalRequests.length && !report.pageErrors.length && !report.consoleErrors.length
      && report.turns.length === (args['chart-prewarm-only'] === 'true' ? 0 : 1 + repeats * SCENARIOS.length) && report.turns.every(turn => turn.assessment.success)
      && report.turns.slice(1).every(turn => turn.residency === 'resident/warm runner' && turn.assessment.targetMet)
      && (args['chart-prewarm'] !== 'true' || report.chartPrewarm?.assessment.targetMet && report.chartPrewarm?.chart.prefixConfirmed)
      && (!report.journeys || report.journeys.exitCode === 0);
    fs.writeFileSync(path.join(output, 'timings.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
    console.log(`Sage browser timing evidence: ${output}; passed=${report.passed}`);
  }
  return report.passed ? 0 : 1;
}

module.exports = { TARGETS_MS, SCENARIOS, loopbackOrigin, parseStream, assessTurn, installVisibleTimer, installWireObserver, prefixWarmEvidence, jsonGet, unusedPort };
if (require.main === module) main().then(code => { process.exitCode = code; }).catch(error => { console.error(error); process.exitCode = 1; });
