'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { after, before, beforeEach, test } = require('node:test');
const { chromium } = require('@playwright/test');

let browser;
let server;
let output;
let driver;

before(async () => {
  output = fs.mkdtempSync(path.join(os.tmpdir(), 'orb-visual-driver-'));
  server = http.createServer((request, response) => {
    if (request.url === '/favicon.ico') {
      response.writeHead(204).end();
      return;
    }
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end(`<!doctype html>
      <html data-theme="dark"><head><title>Synthetic driver regression</title>
      <style>
        body { background: white; color: black; font: 16px sans-serif; }
        button { background: white; color: black; width: 140px; height: 40px; }
      </style></head><body>
      <p id="ready">Synthetic page ready</p><button id="act">Run action</button>
      <script>
        if (location.pathname === '/unmatched-fixture' || location.pathname === '/unmatched-mutation') {
          console.warn('synthetic-unmatched-fixture-warning');
          fetch('/api/unmatched-visual-fixture', {method: location.pathname === '/unmatched-mutation' ? 'POST' : 'GET'}).then(response => {
            document.querySelector('#ready').dataset.fixtureStatus = String(response.status);
          });
        } else {
          console.error('synthetic-startup-console-failure');
          console.warn('synthetic-startup-warning');
          document.querySelector('#act').addEventListener('click', () => {
            console.error('synthetic-action-console-failure');
            console.warn('synthetic-action-warning');
            throw new Error('synthetic-action-page-failure');
          });
        }
      </script></body></html>`);
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });

  // These constants are captured when the real driver is required. Never use
  // an inherited application URL or write synthetic screenshots in the repo.
  const previousBase = process.env.ORB_BASE_URL;
  const previousArgs = process.argv;
  try {
    process.env.ORB_BASE_URL = `http://127.0.0.1:${server.address().port}`;
    process.argv = [process.execPath, __filename, `--out=${output}`];
    driver = require('./driver.cjs');
  } finally {
    process.argv = previousArgs;
    if (previousBase === undefined) delete process.env.ORB_BASE_URL;
    else process.env.ORB_BASE_URL = previousBase;
  }
  browser = await chromium.launch({ headless: true });
});

beforeEach(() => {
  driver.findings.length = 0;
  driver.captures.length = 0;
});

after(async () => {
  try {
    if (browser) await browser.close();
  } finally {
    if (server && server.listening) await new Promise(resolve => server.close(resolve));
    if (output) fs.rmSync(output, { recursive: true, force: true });
  }
});

test('startup and action errors reach the real driver exactly once across themes',
  { timeout: 30_000 }, async () => {
    await driver.runScenario(browser, {
      name: 'regression/console', path: '/', waitFor: '#ready',
      themes: ['dark', 'light'],
      steps: [{ name: 'action', act: async ({ page }) => page.click('#act') }],
    });

    const errors = driver.findings.filter(finding => finding.type === 'console-error');
    assert.deepEqual(errors.map(finding => finding.detail).sort(), [
      'error: synthetic-startup-console-failure',
      'error: synthetic-action-console-failure',
      'pageerror: Error: synthetic-action-page-failure',
    ].sort());
    assert.ok(errors.every(finding => finding.severity === 'major'));
    assert.deepEqual(errors.map(finding => [finding.step, finding.theme]), [
      ['default', 'dark'], ['action', 'dark'], ['action', 'dark'],
    ]);
    assert.ok(!driver.findings.some(finding => finding.detail.includes('synthetic-startup-warning')
      || finding.detail.includes('synthetic-action-warning')));
    assert.ok(!driver.findings.some(finding => finding.type === 'driver-error'));
    assert.equal(driver.captures.length, 4);
    assert.equal(browser.contexts().length, 0);
  });

test('an action failure still flushes errors emitted after the final capture',
  { timeout: 30_000 }, async () => {
    await driver.runScenario(browser, {
      name: 'regression/failed-action', path: '/', waitFor: '#ready', themes: ['dark'],
      steps: [{
        name: 'failed-action',
        act: async ({ page }) => {
          await page.evaluate(() => {
            console.error('synthetic-failed-step-console-failure');
            console.warn('synthetic-failed-step-warning');
          });
          throw new Error('synthetic step failed before capture');
        },
      }],
    });

    const errors = driver.findings.filter(finding => finding.type === 'console-error');
    assert.deepEqual(errors.map(finding => finding.detail).sort(), [
      'error: synthetic-startup-console-failure',
      'error: synthetic-failed-step-console-failure',
    ].sort());
    const flushed = errors.find(finding => finding.detail.includes('synthetic-failed-step'));
    assert.equal(flushed.step, 'teardown');
    assert.ok(!driver.findings.some(finding => finding.detail.includes('synthetic-failed-step-warning')));
    assert.ok(driver.findings.some(finding => finding.type === 'driver-error'
      && finding.detail.includes('synthetic step failed before capture')));
    assert.equal(driver.captures.length, 1);
    assert.equal(browser.contexts().length, 0);
  });

for (const method of ['GET', 'POST']) test(`an unmatched ${method} API fixture survives the network allowance and fails the actual gate`,
  { timeout: 30_000 }, async () => {
    await driver.runScenario(browser, {
      name: 'regression/unmatched-fixture', path: method === 'POST' ? '/unmatched-mutation' : '/unmatched-fixture',
      waitFor: '#ready[data-fixture-status="404"]', themes: ['dark'],
    });

    const detail = `fixture-error: No visual fixture for ${method} /api/unmatched-visual-fixture`;
    const missing = driver.findings.filter(finding => finding.type === 'console-error'
      && finding.detail === detail);
    assert.equal(missing.length, 1);
    assert.equal(missing[0].step, 'default');
    assert.ok(!driver.findings.some(finding => finding.detail.includes('synthetic-unmatched-fixture-warning')));
    assert.ok(!driver.findings.some(finding => finding.type === 'driver-error'));
    assert.equal(driver.captures.length, 1);
    assert.equal(browser.contexts().length, 0);

    fs.writeFileSync(path.join(output, 'findings.json'), JSON.stringify(driver.findings));
    fs.writeFileSync(path.join(output, 'captures.json'), JSON.stringify(driver.captures));
    const gate = spawnSync(process.execPath,
      [path.join(__dirname, 'ci-gate.cjs'), `--in=${output}`],
      { encoding: 'utf8', timeout: 10_000 });
    assert.ifError(gate.error);
    assert.equal(gate.status, 1, `${gate.stdout}\n${gate.stderr}`);
    const summary = JSON.parse(fs.readFileSync(path.join(output, 'gate-summary.json'), 'utf8'));
    assert.equal(summary.gating, 1);
    assert.equal(summary.gate[0].detail, detail);
  });
