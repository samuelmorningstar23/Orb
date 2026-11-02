'use strict';

const assert = require('node:assert/strict');
const { after, before, test } = require('node:test');
const { chromium } = require('@playwright/test');
const { runAudit } = require('./checks.cjs');

let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { if (browser) await browser.close(); });

async function audit(html) {
  const page = await browser.newPage({ viewport: { width: 640, height: 480 } });
  try {
    await page.setContent(`<style>
      body { background: white; color: black; font-family: sans-serif; }
      .sr-only {
        position: absolute; width: 1px; height: 1px; padding: 0;
        margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0);
        white-space: nowrap; border: 0;
      }
    </style>${html}`);
    return await page.evaluate(runAudit);
  } finally {
    await page.close();
  }
}

test('screen reader labels are intentionally clipped', async () => {
  const findings = await audit(`
    <label class="sr-only" for="search">A long accessible search label</label>
    <input id="search" aria-label="Search">
  `);
  assert.deepEqual(findings.filter(f => f.type === 'clipped-text'), []);
});

test('visible nowrap text still reports clipping without ellipsis', async () => {
  const findings = await audit(`
    <p id="visible-clipped" style="width: 60px; overflow: hidden; white-space: nowrap">
      This visible text is genuinely clipped without ellipsis
    </p>
  `);
  const clipped = findings.filter(f => f.type === 'clipped-text');
  assert.equal(clipped.length, 1);
  assert.equal(clipped[0].selector, '#visible-clipped');
});

test('an invisible focusable control remains a finding', async () => {
  const findings = await audit(`
    <button id="invisible-control" style="opacity: 0; width: 100px; height: 32px">
      Save
    </button>
  `);
  assert.ok(findings.some(f => f.type === 'invisible-control'
    && f.selector === '#invisible-control'));
});
