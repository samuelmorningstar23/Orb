/**
 * Orb visual-audit driver.
 *
 * Drives every screen of the app to its real on-screen state, in BOTH themes,
 * across all roles, captures a screenshot per (capture, theme), and runs the
 * checks in checks.cjs inside the page. Two modes:
 *
 *   node tests/visual/driver.cjs                 # mocked mode (fixtures.cjs, auth via localStorage)
 *   node tests/visual/driver.cjs --live          # live smoke vs the real backend (real logins)
 *   node tests/visual/driver.cjs --filter=vigil  # run matching scenarios only
 *   node tests/visual/driver.cjs --headed        # watch it run
 *
 * Outputs (under tests/visual/out/ by default, override with --out=DIR):
 *   findings.json   — every finding from checks.cjs + console/page errors
 *   captures.json   — index of captures (scenario, step, theme, screenshot path)
 *   shots/*.png     — screenshots, named <scenario>--<step>--<theme>.png
 *   gallery.html    — human-readable light/dark gallery with finding counts
 *   The default mocked run also mirrors findings → tests/visual/audit-results.json
 *   and shots → tests/visual/shots/ for the CI artifact upload (ci.yml).
 *
 * Themes: every scenario runs dark + light by default (scenario.themes
 * overrides). The trunk third theme (night / OR) is exercised by the night/*
 * scenarios over a representative surface set.
 *
 * Conventions:
 *   - Panels animate in (fade-in/slide-up up to ~1.1s incl. stagger). settle()
 *     waits for running finite animations to finish before auditing — do not
 *     audit mid-animation (false positives from opacity<1).
 *   - Service workers are blocked (vite-plugin-pwa dev SW would bypass mocks).
 *   - socket.io is aborted in mocked mode. The app makes no external calls
 *     (Orb is strictly offline); doctor/pulse-widget fails if Pulse tries one.
 *   - Theme is exercised in place: audit dark → toggle to light → audit light,
 *     using the real #btn-theme-toggle where the shell provides it.
 */
'use strict';

const fs = require('fs');
const path = require('path');
// CI installs the full `playwright` package (then `npx playwright install
// chromium`); the sandbox image ships `playwright-core` + a system chromium;
// dev machines usually have `@playwright/test`. Take whichever resolves.
let chromium;
try { ({ chromium } = require('playwright')); }
catch {
  try { ({ chromium } = require('playwright-core')); }
  catch { ({ chromium } = require('@playwright/test')); }
}
const { runAudit } = require('./checks.cjs');
const { fixtureFor, NURSE_BOARD_QUIET, NURSE_BOARD_EMPTY } = require('./fixtures.cjs');

const args = process.argv.slice(2);
const LIVE = args.includes('--live');
const HEADED = args.includes('--headed');
const FILTER = (args.find((a) => a.startsWith('--filter=')) || '').replace('--filter=', '');
const OUT = (args.find((a) => a.startsWith('--out=')) || '').replace('--out=', '') ||
  path.join(__dirname, LIVE ? 'out-live' : 'out');
const BASE = process.env.ORB_BASE_URL || 'http://localhost:5173';
const API = process.env.ORB_API_URL || 'http://127.0.0.1:8000';
// Explicit chromium binary: ORB_CHROMIUM / ORB_CHROMIUM_PATH env, else the
// sandbox path if present, else let Playwright resolve its own install (CI).
const EXECUTABLE = process.env.ORB_CHROMIUM || process.env.ORB_CHROMIUM_PATH ||
  (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : null);

const SHOTS = path.join(OUT, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });

// ── users (match server/scripts/seed_clinical_data.py) ─────────────────────
const USERS = {
  doctor:     { id: 'DOC001', name: 'Dr. Meera Sharma',   role: 'doctor',     department: 'General Medicine', password: 'password' },
  surgeon:    { id: 'DOC004', name: 'Dr. Vikram Rathore', role: 'doctor',     department: 'General Surgery',  password: 'password' },
  nurse:      { id: 'NUR001', name: 'Priya Reddy',        role: 'nurse',      department: 'General Medicine', password: 'password' },
  admin:      { id: 'ADM001', name: 'Rajesh Kumar',       role: 'admin',      department: 'Administration',   password: 'password' },
  pharmacist: { id: 'PHA001', name: 'Deepa Joshi',        role: 'pharmacist', department: 'Pharmacy',         password: 'password' },
  patient:    { id: 'P001',   name: 'Raj Patel',          role: 'patient',    password: 'patient123' },
};

const DESKTOP = { width: 1440, height: 900 };

// ── helpers ─────────────────────────────────────────────────────────────────

async function settle(page, extraMs = 120) {
  await page.waitForLoadState('domcontentloaded').catch(() => {});
  await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
  await page.evaluate(async () => {
    const deadline = Date.now() + 4000;
    for (;;) {
      const running = document.getAnimations().filter((a) => {
        try {
          const t = a.effect && a.effect.getTiming ? a.effect.getTiming() : {};
          if (t.iterations === Infinity) return false;
          return a.playState === 'running';
        } catch { return false; }
      });
      if (!running.length || Date.now() > deadline) break;
      await new Promise((r) => setTimeout(r, 100));
    }
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  }).catch(() => {});
  if (extraMs) await page.waitForTimeout(extraMs);
}

async function setTheme(page, theme) {
  const read = () => page.evaluate(() => document.documentElement.getAttribute('data-theme') || 'dark');
  let current = await read();
  if (current === theme) return;
  const toggle = page.locator('#btn-theme-toggle');
  // The shell toggle CYCLES dark → light → night → dark — click until the
  // target theme shows (≤3 clicks). The toggle can also be covered by a modal
  // backdrop or absent (login/public shells) — fall back to JS then.
  if (await toggle.count() && await toggle.isVisible().catch(() => false)) {
    for (let i = 0; i < 3 && current !== theme; i++) {
      const clicked = await toggle.click({ timeout: 1500 }).then(() => true).catch(() => false);
      if (!clicked) break;
      await page.waitForTimeout(150);
      current = await read();
    }
  }
  if (current !== theme) {
    await page.evaluate((t) => {
      document.documentElement.setAttribute('data-theme', t);
      try {
        localStorage.setItem('orb-theme', t);
        localStorage.setItem('orb_theme', t);
      } catch {}
      window.dispatchEvent(new CustomEvent('theme-changed'));
    }, theme);
  }
  // Park the mouse in a neutral corner so no control is left in :hover state
  // (clicking the toggle otherwise leaves it hovered → wrong captured colors).
  await page.mouse.move(2, 2);
  await page.waitForTimeout(450); // let theme transitions + aurora lerp settle
}

function slug(s) {
  return s.replace(/[^a-z0-9-]+/gi, '-').replace(/^-+|-+$/g, '').toLowerCase();
}

// ── scenario runner ─────────────────────────────────────────────────────────

const findings = [];
const captures = [];
let failures = 0;

async function runScenario(browser, scenario) {
  if (FILTER && !scenario.name.includes(FILTER)) return;
  const viewport = scenario.viewport || DESKTOP;
  const context = await browser.newContext({
    viewport,
    serviceWorkers: 'block',
    deviceScaleFactor: 1,
  });

  // Console + page error log with timestamps so we can attribute per capture
  const consoleLog = [];
  context.on('page', (p) => {
    p.on('console', (msg) => {
      if (msg.type() === 'error' || msg.type() === 'warning') {
        consoleLog.push({ t: Date.now(), kind: msg.type(), text: msg.text().slice(0, 500) });
      }
    });
    p.on('pageerror', (err) => {
      consoleLog.push({ t: Date.now(), kind: 'pageerror', text: String(err).slice(0, 500) });
    });
  });

  // Auth + determinism init
  await context.addInitScript(({ user, theme }) => {
    try {
      if (user) {
        localStorage.setItem('orb_token', 'visual-test-token');
        localStorage.setItem('orb_user', JSON.stringify(user));
      }
      localStorage.setItem('orb-theme', theme);
      localStorage.setItem('orb_theme', theme);
    } catch {}
  }, { user: !LIVE && scenario.user ? stripPw(USERS[scenario.user]) : null,
       theme: (scenario.themes && scenario.themes[0]) || 'dark' });

  if (!LIVE) {
    // NOTE: pathname-anchored predicates, NOT '**/api/**' glob — the glob would
    // also swallow Vite dev-server module URLs like /src/api/client.ts.
    // Realtime is not mocked — there is no engine.io server behind these routes,
    // so we refuse the handshake. The error CODE matters: plain `abort()` uses
    // 'failed' → net::ERR_FAILED, which Chromium logs as
    // "Failed to load resource: net::ERR_FAILED" on the console. socket.io then
    // retries the handshake forever (reconnectionDelay 1s → 5s), so that error
    // reappears every few seconds for the whole run and lands in whichever
    // capture windows the retry timer happens to hit — the audit reported it as
    // an app defect against two random scenarios. 'aborted' → net::ERR_ABORTED,
    // which the browser treats as a deliberate client-side cancellation (exactly
    // what it is) and does not log. Same behaviour for the app — the socket
    // still never connects — minus a harness-generated false positive. Do NOT
    // drop the error code.
    await context.route((u) => u.pathname.startsWith('/socket.io'), (r) => r.abort('aborted'));
    await context.route((u) => u.pathname.startsWith('/api/'), async (route) => {
      const req = route.request();
      const u = new URL(req.url());
      // scenario-level route overrides (e.g. forced 401)
      if (scenario.routeOverride) {
        const o = scenario.routeOverride(req.method(), u.pathname);
        if (o) return route.fulfill({ status: o.status, contentType: o.contentType || 'application/json',
          body: typeof o.body === 'string' ? o.body : JSON.stringify(o.body) });
      }
      const fx = fixtureFor(req.method(), u.pathname, u.searchParams);
      if (!fx) {
        return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"no fixture"}' });
      }
      return route.fulfill({
        status: fx.status || 200,
        contentType: fx.contentType || 'application/json',
        body: typeof fx.body === 'string' ? fx.body : JSON.stringify(fx.body),
      });
    });
  }

  const page = await context.newPage();
  // Freeze the clock to the fixtures' epoch (mocked mode) so every relative-time
  // display — the sepsis elapsed timer, "admitted N days ago", the ClockWidget,
  // today-markers — is consistent with the deterministic May-2026 fixture data
  // instead of drifting against the real wall clock.
  if (!LIVE) {
    await page.clock.setFixedTime(new Date('2026-05-23T09:00:00'));
  }
  // window.print must not block the process
  await page.addInitScript(() => { window.print = () => { window.__printCalled = true; }; });

  const ctx = { page, scenario, api: API, base: BASE };

  try {
    if (LIVE && scenario.user) await liveLogin(page, USERS[scenario.user]);
    await page.goto(BASE + (scenario.path || '/'), { waitUntil: 'domcontentloaded' });
    if (scenario.waitFor) await page.waitForSelector(scenario.waitFor, { timeout: 15000 });
    await settle(page);

    const steps = [{ name: 'default', act: null }, ...(scenario.steps || [])];
    for (const step of steps) {
      const capName = step.name === 'default' ? scenario.name : `${scenario.name}--${step.name}`;
      try {
        if (step.act) {
          await step.act(ctx);
          await settle(page);
        }
        for (const theme of scenario.themes || ['dark', 'light']) {
          await setTheme(page, theme);
          await settle(page, 80);
          const consoleStart = consoleLog.length;
          const shotName = `${slug(capName)}--${theme}.png`;
          await page.screenshot({
            path: path.join(SHOTS, shotName),
            fullPage: !scenario.viewportOnly,
            animations: 'disabled',
          }).catch(async () => {
            await page.screenshot({ path: path.join(SHOTS, shotName), animations: 'disabled' });
          });
          let pageFindings = [];
          try {
            pageFindings = await page.evaluate(runAudit);
          } catch (e) {
            pageFindings = [{ type: 'audit-error', severity: 'major', selector: '', snippet: '', detail: String(e).slice(0, 300) }];
          }
          if (scenario.assert) {
            const extra = await scenario.assert(ctx, theme, step.name).catch((e) => ([{
              type: 'assert-error', severity: 'major', selector: '', snippet: '', detail: String(e).slice(0, 300),
            }]));
            if (extra && extra.length) pageFindings.push(...extra);
          }
          const consoleErrors = consoleLog.slice(consoleStart)
            .filter((c) => c.kind !== 'warning')
            .map((c) => ({ type: 'console-error', severity: 'major', selector: '', snippet: '', detail: `${c.kind}: ${c.text}` }));
          for (const f of [...pageFindings, ...consoleErrors]) {
            findings.push({ scenario: scenario.name, step: step.name, capture: capName, theme, mode: LIVE ? 'live' : 'mocked', ...f });
          }
          captures.push({
            capture: capName, scenario: scenario.name, step: step.name, theme,
            shot: `shots/${shotName}`,
            viewport, mode: LIVE ? 'live' : 'mocked',
            findingCount: pageFindings.length + consoleErrors.length,
          });
          process.stdout.write(`  ✓ ${capName} [${theme}] — ${pageFindings.length + consoleErrors.length} findings\n`);
        }
        // return to the scenario's base theme so subsequent step actions run
        // in a consistent theme (night scenarios stay night)
        await setTheme(page, (scenario.themes || ['dark'])[0]);
        await settle(page, 50);
      } catch (e) {
        failures++;
        findings.push({
          scenario: scenario.name, step: step.name, capture: capName, theme: 'n/a',
          mode: LIVE ? 'live' : 'mocked',
          type: 'driver-error', severity: 'critical', selector: '', snippet: '',
          detail: `step failed: ${String(e).slice(0, 400)}`,
        });
        process.stdout.write(`  ✗ ${capName} — ${String(e).split('\n')[0]}\n`);
      }
    }
  } catch (e) {
    failures++;
    findings.push({
      scenario: scenario.name, step: 'setup', capture: scenario.name, theme: 'n/a',
      mode: LIVE ? 'live' : 'mocked',
      type: 'driver-error', severity: 'critical', selector: '', snippet: '',
      detail: `scenario failed: ${String(e).slice(0, 400)}`,
    });
    process.stdout.write(`✗ ${scenario.name} — ${String(e).split('\n')[0]}\n`);
  } finally {
    await context.close();
  }
}

function stripPw(u) {
  const { password, ...rest } = u;
  return rest;
}

async function liveLogin(page, user) {
  const mode = user.role === 'patient' ? 'patient' : 'staff';
  await page.goto(`${BASE}/login/${mode}`, { waitUntil: 'domcontentloaded' });
  if (mode === 'staff') {
    await page.fill('#input-employee-id', user.id);
    await page.fill('#input-password', user.password);
  } else {
    await page.fill('#input-registration', user.id);
    await page.fill('#input-dob', user.password);
  }
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/api/auth/login'), { timeout: 15000 }),
    page.click('#btn-login'),
  ]);
  if (resp.status() !== 200) {
    throw new Error(`live login failed for ${user.id}: HTTP ${resp.status()}. Is the backend seeded?`);
  }
  await page.waitForURL(/dashboard|bridge/, { timeout: 15000 });
}

// ── shared step helpers ─────────────────────────────────────────────────────

const openTab = (tabId, waitSel) => async ({ page }) => {
  await page.click(`#nav-${tabId}`);
  if (waitSel) await page.waitForSelector(waitSel, { timeout: 15000 });
};

/** My Shift's own view tabs (Due now / My patients / Handover / End of shift).
 *  These are inside the panel, not on the shell rail. */
const openNurseView = (viewId, waitSel) => async ({ page }) => {
  await page.click(`#nurse-tab-${viewId}`);
  if (waitSel) await page.waitForSelector(waitSel, { timeout: 15000 });
};

// Semantic assertion: the emergency patient row must be visually distinct from
// a stable row in the CURRENT theme (guards the light-mode glass-card clobber).
async function assertEmergencyDistinct({ page }, theme) {
  const out = [];
  const hasEmergency = await page.locator('.patient-row--emergency').count();
  if (!hasEmergency) {
    out.push({
      type: 'semantic-loss', severity: 'critical', selector: '.patient-row--emergency',
      snippet: 'EMERGENCY row', detail: 'emergency patient row did not render at all (fixture includes an emergency-surgery patient)',
    });
    return out;
  }
  const distinct = await page.evaluate(() => {
    const em = document.querySelector('.patient-row--emergency');
    const normal = Array.from(document.querySelectorAll('.patient-row')).find((r) => !r.classList.contains('patient-row--emergency'));
    if (!em || !normal) return { ok: false, reason: 'rows missing' };
    const a = getComputedStyle(em);
    const b = getComputedStyle(normal);
    const bgDiff = a.backgroundColor !== b.backgroundColor || a.backgroundImage !== b.backgroundImage;
    const borderDiff = a.borderColor !== b.borderColor;
    return { ok: bgDiff || borderDiff, bg: a.backgroundColor, bgImage: a.backgroundImage.slice(0, 80), normalBg: b.backgroundColor };
  });
  if (!distinct.ok) {
    out.push({
      type: 'semantic-loss', severity: 'critical', selector: '.patient-row--emergency',
      snippet: 'EMERGENCY row', detail: `emergency row visually identical to normal row in ${theme} theme (bg ${distinct.bg} vs ${distinct.normalBg})`,
    });
  }
  return out;
}

/**
 * The anti-cry-wolf rule, measured in a real browser with the real cascade.
 *
 * The unit test asserts the class list; only this can assert the COMPUTED
 * colour, in each theme, after the whole stylesheet has run. Two failures it
 * is looking for, and both have shipped before:
 *
 *   1. "OVERDUE" rendered in --status-danger-text. It was red on a quiet shift
 *      and red on a bad one, so the colour carried no information — the exact
 *      mechanism that trains staff to stop reading alerts.
 *   2. The count chip NOT tinted when the count is non-zero, which is the same
 *      bug from the other side: nothing on the screen is escalating.
 *
 * Only runs on the default step, where the fixture guarantees a non-zero
 * overdue count.
 */
async function assertShiftAttention({ page }, theme, step) {
  if (step !== 'default') return [];
  const out = [];
  const seen = await page.evaluate(() => {
    const labels = [...document.querySelectorAll('#panel-shift .orb-section-header__label')];
    const muted = getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim();
    return {
      muted,
      labels: labels.map((el) => ({
        text: (el.firstChild && el.firstChild.textContent || '').trim(),
        colour: getComputedStyle(el).color,
        chip: (() => {
          const c = el.querySelector('.orb-count-chip');
          if (!c) return null;
          return { cls: c.className, colour: getComputedStyle(c).color, bg: getComputedStyle(c).backgroundColor, text: c.textContent };
        })(),
      })),
    };
  });

  if (!seen.labels.length) {
    return [{
      type: 'semantic-loss', severity: 'critical', selector: '#panel-shift .orb-section-header__label',
      snippet: 'section header', detail: `no task section rendered in ${theme} theme (the fixture board has 1 overdue obs and 1 overdue dose)`,
    }];
  }

  for (const label of seen.labels) {
    // Every label must share ONE colour — the neutral one. If a label is
    // coloured per-status they will differ from each other.
    const others = seen.labels.filter((l) => l !== label);
    if (others.some((l) => l.colour !== label.colour)) {
      out.push({
        type: 'semantic-loss', severity: 'critical', selector: '.orb-section-header__label',
        snippet: label.text,
        detail: `section labels do not share one colour in ${theme} theme (${seen.labels.map((l) => `${l.text}=${l.colour}`).join(', ')}) — a label is carrying a status tint, which is the cry-wolf defect`,
      });
      break;
    }
  }

  const overdue = seen.labels.find((l) => /overdue/i.test(l.text));
  if (overdue && overdue.chip && !/orb-count-chip--danger/.test(overdue.chip.cls)) {
    out.push({
      type: 'semantic-loss', severity: 'critical', selector: '.orb-count-chip',
      snippet: overdue.chip.text || '',
      detail: `the overdue count chip is untinted in ${theme} theme — nothing on the screen escalates a non-zero overdue count`,
    });
  }
  if (overdue && overdue.chip && overdue.chip.bg === 'rgba(0, 0, 0, 0)') {
    out.push({
      type: 'semantic-loss', severity: 'major', selector: '.orb-count-chip--danger',
      snippet: overdue.chip.text || '',
      detail: `the overdue count chip resolved to a transparent fill in ${theme} theme (its tint rule did not apply)`,
    });
  }
  return out;
}

// ── Sage ambient asserts (ported from the ambient-sage branch harness) ──────

// The floating Sage orb must be OMNIPRESENT — rendered on every clinician
// surface (the branch's orb_present_on_all gate, docs/assets/sage/
// visual-gate-report.json).
async function assertOrbPresent({ page }, theme) {
  if (await page.locator('.sage-fab').count()) return [];
  return [{
    type: 'semantic-loss', severity: 'critical', selector: '.sage-fab',
    snippet: 'Sage orb',
    detail: `floating Sage orb missing from this surface in ${theme} theme (omnipresence gate)`,
  }];
}

// Vigil must carry the ambient ward brief (narrator snapshot card) AND the
// proactive insights feed.
async function assertWardBriefAndInsights({ page }, theme) {
  const out = [];
  if (!(await page.locator('[data-testid="ward-brief"]').count())) {
    out.push({
      type: 'semantic-loss', severity: 'critical', selector: '[data-testid="ward-brief"]',
      snippet: 'State of the Ward',
      detail: `ward brief card did not render in ${theme} theme (fixture /api/sage/ward-brief supplies a non-null snapshot)`,
    });
  }
  if (!(await page.locator('.sage-insights').count())) {
    out.push({
      type: 'semantic-loss', severity: 'critical', selector: '.sage-insights',
      snippet: 'Sage insights',
      detail: `sage insights feed did not render in ${theme} theme (fixture /api/sage/insights is non-empty)`,
    });
  }
  return out;
}

// The orb is TEXT-ONLY: Orb is strictly offline, so there is no browser
// speech. Clicking it must show the card saying voice is not available, with
// a button into the Sage panel to type, and must never enter a listening state.
async function assertOrbTextOnly({ page }) {
  const out = [];
  const card = page.locator('.sage-fab__card');
  if (!(await card.count())) {
    out.push({
      type: 'semantic-loss', severity: 'critical', selector: '.sage-fab__card',
      snippet: 'voice not available card',
      detail: 'clicking the Sage orb did not surface the "voice is not available" card',
    });
    return out;
  }
  const text = (await card.textContent().catch(() => '')) || '';
  if (!/voice is not available/i.test(text)) {
    out.push({
      type: 'semantic-loss', severity: 'major', selector: '.sage-fab__card',
      snippet: text.slice(0, 50),
      detail: 'orb card rendered, but without saying voice is not available',
    });
  }
  if (!(await page.locator('.sage-fab__open').count())) {
    out.push({
      type: 'semantic-loss', severity: 'major', selector: '.sage-fab__open',
      snippet: 'Open Sage to type',
      detail: 'orb card has no button into the Sage panel',
    });
  }
  if (await page.locator('.sage-fab--listening').count()) {
    out.push({
      type: 'semantic-loss', severity: 'critical', selector: '.sage-fab--listening',
      snippet: 'mic opened',
      detail: 'orb entered a listening state: it is text-only and must never open the microphone',
    });
  }
  return out;
}

// ── mocked scenarios ────────────────────────────────────────────────────────

const MOCKED = [
  { name: 'auth/login-staff', path: '/login/staff', waitFor: '#btn-login' },
  { name: 'auth/login-patient', path: '/login/patient', waitFor: '#btn-login' },

  {
    name: 'doctor/vigil', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    assert: (ctx, theme, step) => (step === 'default' ? assertEmergencyDistinct(ctx, theme) : Promise.resolve([])),
    steps: [
      {
        name: 'detail',
        act: async ({ page }) => {
          await page.locator('.patient-row').first().click();
          await page.waitForSelector('.command-bar, .patient-detail', { timeout: 15000 });
        },
      },
      {
        name: 'meds-modal',
        act: async ({ page }) => {
          await page.click('#btn-meds-reports');
          await page.waitForSelector('.modal-overlay', { timeout: 10000 });
        },
      },
      {
        name: 'discharge-modal',
        act: async ({ page }) => {
          // close meds modal first (click its Close button or overlay edge)
          await page.keyboard.press('Escape').catch(() => {});
          const overlay = page.locator('.modal-overlay');
          if (await overlay.count()) {
            const closeBtn = page.locator('.modal-overlay .modal__close, .modal-overlay button:has-text("Close")').first();
            if (await closeBtn.count()) await closeBtn.click();
            else await page.mouse.click(10, 10);
          }
          await page.waitForTimeout(300);
          await page.click('button:has-text("DISCHARGE")');
          await page.waitForSelector('.modal-overlay', { timeout: 10000 });
        },
      },
      {
        name: 'print-record',
        act: async ({ page }) => {
          // close discharge modal
          const cancel = page.locator('.modal-overlay button:has-text("Cancel")').first();
          if (await cancel.count()) await cancel.click();
          await page.waitForTimeout(300);
          await page.click('button:has-text("PRINT RECORD")');
          // The portal is display:none on screen (hidden until print media),
          // so emulate print FIRST, then wait for it to become visible.
          await page.emulateMedia({ media: 'print' });
          await page.waitForSelector('.discharge-print-layout', { state: 'visible', timeout: 10000 });
        },
      },
      {
        name: 'after-print',
        act: async ({ page }) => {
          await page.emulateMedia({ media: 'screen' });
          await page.waitForTimeout(200);
        },
      },
    ],
  },

  {
    name: 'doctor/orders', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      {
        name: 'list',
        act: async ({ page }) => {
          await page.click('#nav-orders');
          await page.fill('#panel-orders input[placeholder="e.g. PAT001"]', 'PAT001');
          await page.waitForTimeout(600);
        },
      },
      {
        name: 'new-order-form',
        act: async ({ page }) => {
          await page.click('#panel-orders button:has-text("New Order")');
          await page.waitForTimeout(300);
        },
      },
    ],
  },

  {
    name: 'doctor/relay', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      { name: 'channels', act: openTab('relay', '.relay-channel-item') },
      {
        name: 'room',
        act: async ({ page }) => {
          await page.locator('.relay-channel-item').first().click();
          await page.waitForSelector('.relay-chat__header', { timeout: 15000 });
        },
      },
    ],
  },

  {
    name: 'doctor/sage', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [{ name: 'panel', act: openTab('sage', '#panel-sage') }],
  },
  {
    name: 'doctor/scribe', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [{ name: 'panel', act: openTab('scribe', '#panel-scribe') }],
  },
  {
    name: 'doctor/lens', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [{ name: 'panel', act: openTab('lens', '#panel-lens') }],
  },
  {
    name: 'doctor/forecast', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      { name: 'census', act: openTab('forecast', '#panel-forecast') },
      { name: 'capacity', act: async ({ page }) => { await page.click('#panel-forecast button:has-text("Ward Capacity")'); } },
      { name: 'discharge-board', act: async ({ page }) => { await page.click('#panel-forecast button:has-text("Discharge Board")'); } },
      { name: 'admissions', act: async ({ page }) => { await page.click('#panel-forecast button:has-text("Admission Patterns")'); } },
    ],
  },
  {
    name: 'doctor/command', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [{ name: 'panel', act: openTab('command', '#panel-command') }],
  },
  {
    name: 'doctor/capacity', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [{ name: 'panel', act: openTab('capacity', '#panel-capacity') }],
  },
  {
    name: 'doctor/coding', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      {
        name: 'analysis',
        act: async ({ page }) => {
          await page.click('#nav-coding');
          const input = page.locator('#panel-coding input').first();
          await input.fill('PAT001');
          await page.click('#panel-coding button:has-text("Analyze")');
          await page.waitForTimeout(600);
        },
      },
    ],
  },
  {
    name: 'doctor/appointments', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [{ name: 'panel', act: openTab('appointments', '#panel-appointments') }],
  },
  {
    name: 'doctor/admit-modal', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      {
        name: 'identity',
        act: async ({ page }) => {
          await page.click('#btn-admit-patient');
          await page.waitForSelector('.admit-modal', { timeout: 10000 });
        },
      },
      {
        name: 'vitals-tab',
        act: async ({ page }) => {
          await page.click('.admit-modal__tab:has-text("Vitals")');
          await page.waitForTimeout(250);
        },
      },
      {
        name: 'review-tab',
        act: async ({ page }) => {
          // trunk added a Review tab (last tab was clipped once — keep covered)
          const tab = page.locator('.admit-modal__tab:has-text("Review")').first();
          if (await tab.count()) await tab.click();
          else await page.getByRole('button', { name: 'Review', exact: false }).first().click().catch(() => {});
          await page.waitForTimeout(250);
        },
      },
      {
        name: 'escape-closes',
        act: async ({ page }) => {
          await page.keyboard.press('Escape');
          await page.waitForTimeout(400);
          const still = await page.locator('.admit-modal').count();
          if (still) throw new Error('Escape did not close the Admit modal');
        },
      },
    ],
  },
  {
    // Pulse: season and conditions, from the calendar built into Orb. Strictly
    // offline: opening it may ask the app's own origin and nothing else (the
    // old panel called nominatim.openstreetmap.org from the browser). Waits for
    // the header trigger, not a patient row: the doctor now lands on Today.
    name: 'doctor/pulse-widget', user: 'doctor', path: '/dashboard', waitFor: '#btn-pulse',
    steps: [
      {
        name: 'open',
        act: async ({ page }) => {
          const appOrigin = new URL(page.url()).origin;
          const offOrigin = [];
          const watch = (req) => {
            const u = new URL(req.url());
            if ((u.protocol === 'http:' || u.protocol === 'https:') && u.origin !== appOrigin) offOrigin.push(req.url());
          };
          page.on('request', watch);
          try {
            await page.click('#btn-pulse');
            await page.waitForSelector('.pulse-panel', { timeout: 10000 });
            // Loaded: the season cards (or, live, the setup or error block).
            await page.waitForSelector('.pulse-panel .pulse-season, .pulse-panel .orb-state--empty, .pulse-panel .orb-state--error', { timeout: 10000 });
          } finally {
            page.off('request', watch);
          }
          if (offOrigin.length) throw new Error(`Pulse asked another origin: ${offOrigin.join(', ')}`);
        },
      },
    ],
  },

  // ── trunk wave-1/2 surfaces (ported from the trunk harness) ──────────────
  {
    name: 'doctor/patient-story', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      {
        name: 'news2-why',
        act: async ({ page }) => {
          await page.locator('.patient-row').first().click();
          await page.waitForSelector('.command-bar, .patient-detail', { timeout: 15000 });
          // Explainable NEWS2: expand the per-parameter "why this score" panel.
          const toggle = page.locator('.news2-attribution__toggle').first();
          if (await toggle.count()) await toggle.click().catch(() => {});
          await page.waitForTimeout(300);
        },
      },
      {
        name: 'story',
        act: async ({ page }) => {
          // Unified Patient Story — merged clinical narrative + filter chips.
          await page.click('#btn-story');
          await page.waitForSelector('.story-modal', { timeout: 10000 });
        },
      },
    ],
  },
  {
    name: 'doctor/emar-safety', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      {
        name: 'administer-modal',
        act: async ({ page }) => {
          await page.locator('.patient-row').first().click();
          await page.waitForSelector('.command-bar, .patient-detail', { timeout: 15000 });
          // The med table (eMAR chips + Administer) lives INSIDE the
          // Medications modal opened from the detail command bar.
          await page.click('#btn-meds-reports');
          await page.waitForSelector('.modal-overlay', { timeout: 10000 });
          // AdministerModal: five rights + type-to-confirm.
          await page.locator('button:has-text("Administer")').first().click();
          await page.waitForSelector('.administer-modal', { timeout: 10000 });
        },
      },
      {
        name: 'safety-ack-modal',
        act: async ({ page }) => {
          await page.keyboard.press('Escape'); // close AdministerModal (meds modal stays)
          await page.waitForTimeout(300);
          // Blocking SafetyAlertModal: the med-add POST fixture answers 428.
          await page.locator('.detail__add-btn').first().click();
          await page.waitForTimeout(300);
          const inputs = page.locator('.modal input[type="text"]');
          await inputs.nth(0).fill('Amoxicillin');
          await inputs.nth(1).fill('500 mg');
          await inputs.nth(2).fill('TDS');
          await page.locator('.modal__confirm').last().click();
          await page.waitForSelector('.safety-modal-backdrop', { timeout: 10000 });
        },
      },
    ],
  },
  {
    name: 'doctor/mfa-palette', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      {
        name: 'mfa-enroll',
        act: async ({ page }) => {
          await page.click('#btn-mfa-enroll');
          await page.waitForSelector('.mfa-modal', { timeout: 10000 });
        },
      },
      {
        name: 'command-palette',
        act: async ({ page }) => {
          await page.keyboard.press('Escape');
          await page.waitForTimeout(300);
          await page.keyboard.press('Control+k');
          await page.waitForSelector('.cmdk-panel', { timeout: 5000 });
          await page.keyboard.type('mar');
          await page.waitForTimeout(500); // 200ms debounce + mocked fetch
        },
      },
    ],
  },

  {
    name: 'surgeon/suite', user: 'surgeon', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      { name: 'schedule', act: openTab('surgical-suite', '#panel-surgical-suite') },
      {
        name: 'day',
        act: async ({ page }) => {
          const card = page.locator('.suite__day-card').first();
          if (await card.count()) {
            await card.click();
            await page.waitForTimeout(700);
          }
        },
      },
      {
        name: 'active-cases',
        act: async ({ page }) => {
          // day view (tier 2) hides the view tabs — go back to the week first
          const back = page.locator('#suite-back-week');
          if (await back.count()) {
            await back.click();
            await page.waitForTimeout(600);
          }
          await page.click('#panel-surgical-suite button:has-text("Active Cases")');
          await page.waitForTimeout(500);
        },
      },
    ],
  },

  /* ── NURSE — "My Shift" ─────────────────────────────────────────────────
     Until now this surface had NEVER been captured, in any theme. The single
     nurse scenario below waited on `.patient-row`, which is Vigil's roster —
     but a nurse's landing tab is `shift` (shellConfig.tsx), so the wait was
     against a selector that does not exist on the page it opens. The nurse
     home has been ungated since it was built, and an ungated surface in a
     three-theme system regresses within two weeks.

     The states are captured separately and deliberately. A populated board
     proves one sixth of this screen; the calm empty state ("nothing due" as
     GOOD NEWS, not as a blank) and the read-failure state ("this is a display
     failure, not an all-clear") are the two that decide whether a nurse trusts
     it, and neither is reachable from the default fixture. */
  {
    name: 'nurse/shift', user: 'nurse', path: '/dashboard', waitFor: '#panel-shift .orb-row',
    assert: assertShiftAttention,
    steps: [
      { name: 'patients', act: openNurseView('patients', '.patient-card') },
      { name: 'handover', act: openNurseView('handover', '.handover__item') },
      { name: 'end-of-shift', act: openNurseView('shift', '.shift-window') },
    ],
  },

  /* The most important state on either home. It has to read as reassurance —
     a solid card, one green check, the next due time named — and never as the
     dashed grey box that also means "we found nothing". */
  {
    name: 'nurse/shift-nothing-due', user: 'nurse', path: '/dashboard',
    waitFor: '#panel-shift .orb-state--calm',
    routeOverride: (method, pathname) =>
      (method === 'GET' && pathname === '/api/nurse/board' ? { body: NURSE_BOARD_QUIET } : null),
  },

  /* Absence, not achievement — and it must not wear the calm treatment. */
  {
    name: 'nurse/shift-no-patients', user: 'nurse', path: '/dashboard',
    waitFor: '#panel-shift .orb-state--empty:not(.orb-state--calm)', themes: ['dark'],
    routeOverride: (method, pathname) =>
      (method === 'GET' && pathname === '/api/nurse/board' ? { body: NURSE_BOARD_EMPTY } : null),
    steps: [{ name: 'patients', act: openNurseView('patients', '.orb-state--empty:not(.orb-state--calm)') }],
  },

  /* A failed read must never render as an empty queue. */
  {
    name: 'nurse/shift-read-failed', user: 'nurse', path: '/dashboard',
    waitFor: '#panel-shift .orb-state--error', themes: ['dark', 'light'],
    routeOverride: (method, pathname) =>
      (method === 'GET' && pathname === '/api/nurse/board'
        ? { status: 500, body: { detail: 'Shift board unavailable (500)' } } : null),
  },

  {
    name: 'nurse/vigil', user: 'nurse', path: '/dashboard', waitFor: '#panel-shift',
    steps: [{ name: 'roster', act: openTab('vigil', '.patient-row') }],
  },

  {
    name: 'admin/panel', user: 'admin', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      { name: 'overview', act: openTab('admin', '#admin-tab-overview') },
      { name: 'users', act: async ({ page }) => { await page.click('#admin-tab-users'); await page.waitForSelector('.admin-table, .admin-empty', { timeout: 10000 }); } },
      { name: 'audit-logs', act: async ({ page }) => { await page.click('#admin-tab-audit'); await page.waitForSelector('.admin-table, .admin-empty', { timeout: 10000 }); } },
      {
        name: 'trace',
        act: async ({ page }) => {
          await page.click('#admin-tab-trace');
          const input = page.locator('#panel-admin input[placeholder*="correlation" i]').first();
          if (await input.count()) {
            await input.fill('corr-0001');
            await page.click('#panel-admin button:has-text("Trace")');
            await page.waitForTimeout(500);
          }
        },
      },
      { name: 'ingestion', act: async ({ page }) => { await page.click('#admin-tab-ingestion'); await page.waitForTimeout(400); } },
      { name: 'security', act: async ({ page }) => { await page.click('#admin-tab-security'); await page.waitForTimeout(400); } },
      { name: 'alerts', act: async ({ page }) => { await page.click('#admin-tab-alerts'); await page.waitForTimeout(400); } },
      { name: 'models', act: async ({ page }) => { await page.click('#admin-tab-models'); await page.waitForTimeout(400); } },
      { name: 'impact', act: async ({ page }) => { await page.click('#admin-tab-impact'); await page.waitForTimeout(600); } },
      { name: 'trust-center', act: async ({ page }) => { await page.click('#admin-tab-trust'); await page.waitForTimeout(600); } },
      { name: 'flight-recorder', act: async ({ page }) => { await page.click('#admin-tab-flight'); await page.waitForTimeout(600); } },
      { name: 'data-backups', act: async ({ page }) => { await page.click('#admin-tab-data'); await page.waitForSelector('.admin-table, .admin-empty', { timeout: 10000 }); } },
    ],
  },

  {
    name: 'pharmacist/helix', user: 'pharmacist', path: '/dashboard', waitFor: '#panel-helix',
    steps: [
      { name: 'overview', act: async ({ page }) => { await page.waitForSelector('#panel-helix button:has-text("Overview")', { timeout: 15000 }); } },
      { name: 'patient-board', act: async ({ page }) => { await page.click('#panel-helix button:has-text("Patient Board")'); await page.waitForTimeout(400); } },
      { name: 'formulary', act: async ({ page }) => { await page.click('#panel-helix button:has-text("Formulary")'); await page.waitForTimeout(400); } },
      {
        name: 'order-review-modal',
        act: async ({ page }) => {
          await page.click('#panel-helix button:has-text("Overview")');
          await page.waitForTimeout(400);
          const row = page.locator('#panel-helix table tbody tr').first();
          if (await row.count()) {
            // plain .click() can hang in "scrolling into view" on this table —
            // dispatch the click on the element directly
            await row.evaluate((el) => el.click());
            await page.waitForSelector('[role="dialog"]', { timeout: 8000 });
          }
        },
      },
    ],
  },
  {
    name: 'pharmacist/verify-queue', user: 'pharmacist', path: '/dashboard', waitFor: '#panel-helix',
    steps: [{ name: 'queue', act: openTab('pharmacy-verify', '#panel-pharmacy-verify') }],
  },
  {
    name: 'pharmacist/sage', user: 'pharmacist', path: '/dashboard', waitFor: '#panel-helix',
    steps: [{ name: 'panel', act: openTab('sage', '#panel-sage') }],
  },

  {
    name: 'patient/bridge', user: 'patient', path: '/bridge', waitFor: '#bridge-logout',
    steps: [
      {
        name: 'meds-modal',
        act: async ({ page }) => {
          await page.click('#bridge-btn-meds');
          await page.waitForSelector('.modal-overlay', { timeout: 8000 });
        },
      },
      {
        name: 'docs-modal',
        act: async ({ page }) => {
          await page.click('.modal-overlay button:has-text("Close")');
          await page.waitForTimeout(400);
          await page.click('#bridge-btn-docs');
          await page.waitForSelector('.modal-overlay', { timeout: 8000 });
        },
      },
      {
        name: 'labs-modal',
        act: async ({ page }) => {
          await page.click('.modal-overlay button:has-text("Close")');
          await page.waitForTimeout(400);
          await page.click('#bridge-btn-labs');
          await page.waitForSelector('.modal-overlay', { timeout: 8000 });
        },
      },
      {
        name: 'discharge-modal',
        act: async ({ page }) => {
          await page.click('.modal-overlay button:has-text("Close")');
          await page.waitForTimeout(400);
          await page.click('#bridge-btn-discharge');
          await page.waitForSelector('.modal-overlay', { timeout: 8000 });
        },
      },
      {
        name: 'ask',
        act: async ({ page }) => {
          await page.click('.modal-overlay button:has-text("Close")');
          await page.waitForTimeout(400);
          await page.fill('#bridge-ask-input', 'When can I go home?');
          await page.click('#bridge-ask-btn');
          await page.waitForSelector('.bridge__answer', { timeout: 10000 });
        },
      },
      {
        name: 'book-appointment',
        act: async ({ page }) => {
          // pick an enabled (future) date first — slots render after a date
          await page.locator('.appt__week-cell:not([disabled])').last().click();
          await page.waitForTimeout(400);
          const slot = page.locator('.appt__slot').first();
          if (await slot.count()) await slot.click();
          const dept = page.locator('.appt__dept').first();
          if (await dept.count()) await dept.click();
          await page.click('#appt-book-btn');
          await page.waitForTimeout(2600); // confirm animation ~1.8s + check draw
        },
      },
    ],
  },

  {
    name: 'public/discharge-access', path: '/discharge/tok-visual-test', waitFor: '#dob',
    steps: [
      {
        name: 'verified',
        act: async ({ page }) => {
          await page.fill('#dob', '1980-01-15');
          await page.click('.verify-btn');
          await page.waitForSelector('.discharge-print-layout, .verification-error', { timeout: 10000 });
        },
      },
    ],
  },

  {
    name: 'staff/downtime', user: 'doctor', path: '/downtime/PAT001', waitFor: '.downtime-mode, .dt-toolbar, body',
    steps: [
      {
        name: 'print-emulated',
        act: async ({ page }) => { await page.emulateMedia({ media: 'print' }); },
      },
      {
        name: 'back-to-screen',
        act: async ({ page }) => { await page.emulateMedia({ media: 'screen' }); },
      },
    ],
  },
  {
    name: 'staff/handoff', user: 'doctor', path: '/handoff/PAT001', waitFor: 'body',
    steps: [
      {
        name: 'print-emulated',
        act: async ({ page }) => { await page.emulateMedia({ media: 'print' }); },
      },
      {
        name: 'back-to-screen',
        act: async ({ page }) => { await page.emulateMedia({ media: 'screen' }); },
      },
    ],
  },

  {
    name: 'errors/forced-401', user: 'doctor', path: '/dashboard', waitFor: 'body',
    themes: ['dark'],
    routeOverride: (method, pathname) =>
      pathname === '/api/vigil/patients' ? { status: 401, body: { detail: 'Session revoked; please log in again' } } : null,
    steps: [
      {
        name: 'result',
        act: async ({ page }) => {
          // client clears the token and reloads to the login screen
          await page.waitForURL(/login/, { timeout: 15000 }).catch(() => {});
          await page.waitForSelector('#btn-login', { timeout: 15000 }).catch(() => {});
        },
      },
    ],
  },

  {
    // Trunk behavior: a mid-session 401 dispatches orb:session-expired and a
    // blocking glass re-auth modal preserves unsaved work (vs the login
    // redirect exercised by errors/forced-401 above).
    name: 'errors/session-expired', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      {
        name: 'modal',
        act: async ({ page }) => {
          // Later-registered routes win, so this 401 override outranks the
          // fixture route for every subsequent /api call.
          await page.context().route((u) => u.pathname.startsWith('/api/'), (r) =>
            r.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"Token expired"}' }));
          await page.click('#nav-relay').catch(() => {});
          await page.click('#nav-vigil').catch(() => {});
          await page.waitForSelector('.session-expired-modal', { timeout: 15000 });
        },
      },
    ],
  },

  // ── Sage ambient intelligence (ported from the ambient-sage branch
  //    harness — docs/assets/sage/visual-gate-report.json). Three gates:
  //    (1) the floating orb is omnipresent on every clinician surface,
  //    (2) Vigil carries the ward brief + proactive insights feed,
  //    (3) the orb is text-only: its click shows the "voice is not available"
  //        card with a way into the Sage panel, and never opens the mic.
  //    (The branch's sage-panel light/dark captures are already covered by
  //    doctor/sage above, which runs both themes by default.) ──
  {
    name: 'sage/orb-omnipresence', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    assert: (ctx, theme) => assertOrbPresent(ctx, theme),
    steps: [
      { name: 'relay', act: openTab('relay', '.relay-channel-item') },
      { name: 'orders', act: openTab('orders', '#panel-orders') },
      { name: 'sage', act: openTab('sage', '#panel-sage') },
      { name: 'scribe', act: openTab('scribe', '#panel-scribe') },
      { name: 'lens', act: openTab('lens', '#panel-lens') },
      { name: 'forecast', act: openTab('forecast', '#panel-forecast') },
    ],
  },
  {
    name: 'sage/ward-brief-insights', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    assert: (ctx, theme) => assertWardBriefAndInsights(ctx, theme),
  },
  {
    // Captured dark-only, matching the branch's voice-governance-gate-dark
    // doc asset. Waits for the orb itself: the doctor now lands on Today,
    // which has no .patient-row.
    name: 'sage/orb-text-only', user: 'doctor', path: '/dashboard', waitFor: '.sage-fab__orb',
    themes: ['dark'],
    assert: (ctx, theme, step) =>
      (step === 'text-only-card' ? assertOrbTextOnly(ctx) : Promise.resolve([])),
    steps: [
      {
        name: 'text-only-card',
        act: async ({ page }) => {
          // patient-scoped surface (orders), as in the branch capture
          await page.click('#nav-orders');
          await page.waitForSelector('#panel-orders', { timeout: 15000 });
          await page.click('.sage-fab__orb');
          await page.waitForSelector('.sage-fab__card', { timeout: 8000 });
        },
      },
    ],
  },

  // ── night / OR theme — representative-surface pass (trunk third theme).
  //    The night theme retokenizes every surface; rather than re-run the whole
  //    suite a third time, drive a representative set spanning surface types
  //    so the contrast/visibility gate validates the token system on
  //    true-black in each kind of surface. ──
  { name: 'night/login-staff', path: '/login/staff', waitFor: '#btn-login', themes: ['night'] },
  {
    name: 'night/doctor', user: 'doctor', path: '/dashboard', waitFor: '.patient-row', themes: ['night'],
    steps: [
      {
        name: 'detail',
        act: async ({ page }) => {
          await page.locator('.patient-row').first().click();
          await page.waitForSelector('.command-bar, .patient-detail', { timeout: 15000 });
        },
      },
      { name: 'forecast', act: openTab('forecast', '#panel-forecast') },
      { name: 'relay', act: openTab('relay', '.relay-channel-item') },
    ],
  },
  {
    name: 'night/admin-overview', user: 'admin', path: '/dashboard', waitFor: '.patient-row', themes: ['night'],
    steps: [{ name: 'overview', act: openTab('admin', '#admin-tab-overview') }],
  },
  {
    name: 'night/pharmacy-verify', user: 'pharmacist', path: '/dashboard', waitFor: '#panel-helix', themes: ['night'],
    steps: [{ name: 'queue', act: openTab('pharmacy-verify', '#panel-pharmacy-verify') }],
  },
  /* My Shift at 03:00 is not a hypothetical: night duty is when this screen is
     read most and when the one [data-theme="night"] border override on the row
     chassis has to do its whole job — night's canvas is #000000 and its card
     surface #101010, so the 1px border is the ONLY thing separating a task row
     from the page. A nurse should be able to tell a quiet ward from a busy one
     from three metres away in the dark, so both are captured. */
  {
    name: 'night/nurse-shift', user: 'nurse', path: '/dashboard',
    waitFor: '#panel-shift .orb-row', themes: ['night'],
    assert: assertShiftAttention,
    steps: [{ name: 'patients', act: openNurseView('patients', '.patient-card') }],
  },
  {
    name: 'night/nurse-shift-nothing-due', user: 'nurse', path: '/dashboard',
    waitFor: '#panel-shift .orb-state--calm', themes: ['night'],
    routeOverride: (method, pathname) =>
      (method === 'GET' && pathname === '/api/nurse/board' ? { body: NURSE_BOARD_QUIET } : null),
  },

  // responsive spot-checks
  { name: 'responsive/login-phone', path: '/login/staff', waitFor: '#btn-login', viewport: { width: 390, height: 844 } },
  { name: 'responsive/vigil-tablet', user: 'doctor', path: '/dashboard', waitFor: '.patient-row', viewport: { width: 834, height: 1194 } },
  { name: 'responsive/vigil-phone', user: 'doctor', path: '/dashboard', waitFor: '.patient-row', viewport: { width: 390, height: 844 } },
  { name: 'responsive/bridge-phone', user: 'patient', path: '/bridge', waitFor: '#bridge-logout', viewport: { width: 390, height: 844 } },
  /* 768×1024 is the bedside device, not a spot-check: it is where the row's
     trailing lateness drops below the body, the tab strip starts scrolling and
     every target has to clear 44px. 390 covers the Relay-in-pocket case. */
  {
    name: 'responsive/nurse-shift-tablet', user: 'nurse', path: '/dashboard',
    waitFor: '#panel-shift .orb-row', viewport: { width: 768, height: 1024 }, themes: ['dark'],
    steps: [{ name: 'patients', act: openNurseView('patients', '.patient-card') }],
  },
  {
    name: 'responsive/nurse-shift-phone', user: 'nurse', path: '/dashboard',
    waitFor: '#panel-shift .orb-row', viewport: { width: 390, height: 844 }, themes: ['dark'],
  },
];

// ── live smoke scenarios ────────────────────────────────────────────────────

const LIVE_SCENARIOS = [
  {
    name: 'live/doctor-dashboard', user: 'doctor', path: '/dashboard', waitFor: '.patient-row',
    steps: [
      {
        name: 'patient-detail',
        act: async ({ page }) => {
          await page.locator('.patient-row').first().click();
          await page.waitForSelector('.command-bar, .patient-detail', { timeout: 20000 });
        },
      },
      { name: 'forecast', act: openTab('forecast', '#panel-forecast') },
      { name: 'command', act: openTab('command', '#panel-command') },
      { name: 'capacity', act: openTab('capacity', '#panel-capacity') },
      { name: 'appointments', act: openTab('appointments', '#panel-appointments') },
      {
        name: 'relay-search',
        act: async ({ page }) => {
          await page.click('#nav-relay');
          const search = page.locator('#panel-relay input[placeholder*="Search" i]').first();
          await search.waitFor({ timeout: 15000 });
          await search.fill('Case');
          await page.waitForTimeout(500);
        },
      },
    ],
  },
  {
    name: 'live/admin-panel', user: 'admin', path: '/dashboard', waitFor: '#nav-admin',
    steps: [
      { name: 'overview', act: openTab('admin', '#admin-tab-overview') },
      { name: 'users', act: async ({ page }) => { await page.click('#admin-tab-users'); await page.waitForSelector('.admin-table, .admin-empty', { timeout: 15000 }); } },
      { name: 'data-backups', act: async ({ page }) => { await page.click('#admin-tab-data'); await page.waitForSelector('.admin-table, .admin-empty', { timeout: 15000 }); } },
      {
        name: 'audit-paginated',
        act: async ({ page }) => {
          await page.click('#admin-tab-audit');
          await page.waitForSelector('.admin-table, .admin-empty', { timeout: 15000 });
          const next = page.locator('.admin-page-btn:has-text("Next"), .admin-page-btn').last();
          if (await next.count() && await next.isEnabled().catch(() => false)) {
            await next.click();
            await page.waitForTimeout(800);
          }
        },
      },
      { name: 'trust-center', act: async ({ page }) => { await page.click('#admin-tab-trust'); await page.waitForTimeout(1200); } },
      { name: 'impact', act: async ({ page }) => { await page.click('#admin-tab-impact'); await page.waitForTimeout(1200); } },
      { name: 'flight-recorder', act: async ({ page }) => { await page.click('#admin-tab-flight'); await page.waitForTimeout(1200); } },
    ],
  },
  {
    name: 'live/pharmacist', user: 'pharmacist', path: '/dashboard', waitFor: '#panel-helix',
    steps: [
      { name: 'verify-queue', act: openTab('pharmacy-verify', '#panel-pharmacy-verify') },
    ],
  },
  {
    name: 'live/patient-bridge', user: 'patient', path: '/bridge', waitFor: '#bridge-logout',
    steps: [
      {
        name: 'book-appointment',
        act: async ({ page }) => {
          await page.locator('.appt__week-cell:not([disabled])').last().click();
          await page.waitForTimeout(400);
          const slot = page.locator('.appt__slot').first();
          if (await slot.count()) await slot.click();
          const dept = page.locator('.appt__dept').first();
          if (await dept.count()) await dept.click();
          await page.click('#appt-book-btn');
          await page.waitForTimeout(2600);
        },
      },
      {
        name: 'records',
        act: async ({ page }) => {
          await page.click('#bridge-btn-meds');
          await page.waitForSelector('.modal-overlay', { timeout: 10000 });
        },
      },
    ],
  },
];

// ── main ────────────────────────────────────────────────────────────────────

(async () => {
  const browser = await chromium.launch(
    EXECUTABLE ? { headless: !HEADED, executablePath: EXECUTABLE } : { headless: !HEADED });
  const scenarios = LIVE ? LIVE_SCENARIOS : MOCKED;
  const t0 = Date.now();
  for (const s of scenarios) {
    process.stdout.write(`▶ ${s.name}\n`);
    await runScenario(browser, s);
  }
  await browser.close();

  fs.writeFileSync(path.join(OUT, 'findings.json'), JSON.stringify(findings, null, 2));
  fs.writeFileSync(path.join(OUT, 'captures.json'), JSON.stringify(captures, null, 2));
  writeGallery();

  // CI compatibility: .github/workflows/ci.yml uploads tests/visual/shots/ and
  // tests/visual/audit-results.json as build artifacts. Mirror the default
  // (mocked, non-live) run's outputs onto those stable paths; OUT/ stays the
  // canonical location documented above.
  if (!LIVE) {
    fs.writeFileSync(path.join(__dirname, 'audit-results.json'), JSON.stringify(findings, null, 2));
    const ciShots = path.join(__dirname, 'shots');
    if (path.resolve(SHOTS) !== path.resolve(ciShots)) {
      fs.rmSync(ciShots, { recursive: true, force: true });
      fs.cpSync(SHOTS, ciShots, { recursive: true });
    }
  }

  const byType = {};
  for (const f of findings) byType[f.type] = (byType[f.type] || 0) + 1;
  console.log(`\n${captures.length} captures, ${findings.length} raw findings in ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  console.log(Object.entries(byType).map(([k, v]) => `  ${k}: ${v}`).join('\n'));
  console.log(`→ ${OUT}/findings.json · captures.json · gallery.html`);
  if (failures) {
    console.log(`\n⚠ ${failures} scenario/step failures — see driver-error findings`);
  }
})();

function writeGallery() {
  const groups = {};
  for (const c of captures) {
    (groups[c.capture] = groups[c.capture] || []).push(c);
  }
  const rows = Object.entries(groups).map(([name, caps]) => {
    const dark = caps.find((c) => c.theme === 'dark');
    const light = caps.find((c) => c.theme === 'light');
    const cell = (c) => c
      ? `<figure><a href="${c.shot}" target="_blank"><img loading="lazy" src="${c.shot}" alt="${name} ${c.theme}"></a><figcaption>${c.theme} · ${c.findingCount} findings</figcaption></figure>`
      : '<figure class="missing">—</figure>';
    return `<section><h2>${name}</h2><div class="pair">${cell(dark)}${cell(light)}</div></section>`;
  }).join('\n');
  const html = `<!doctype html><meta charset="utf-8"><title>Orb visual audit gallery</title>
<style>
  body{font:14px/1.5 system-ui;margin:24px;background:#111;color:#eee}
  h1{font-size:20px} h2{font-size:13px;font-weight:600;margin:18px 0 6px;color:#bbb}
  .pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  figure{margin:0;background:#1a1a1f;border:1px solid #333;border-radius:8px;padding:8px}
  img{width:100%;height:auto;border-radius:4px;display:block}
  figcaption{font-size:11px;color:#999;margin-top:6px}
</style>
<h1>Orb visual audit — ${LIVE ? 'live' : 'mocked'} · ${captures.length} captures</h1>
${rows}`;
  fs.writeFileSync(path.join(OUT, 'gallery.html'), html);
}
