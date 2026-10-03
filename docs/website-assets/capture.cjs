#!/usr/bin/env node
/*
 * Orb website assets: capture every module screen and the demo workflows from the
 * LIVE app (real logins, real seeded data, no mocks).
 *
 *   node docs/website-assets/capture.cjs                # gallery (dark + light) + workflows
 *   node docs/website-assets/capture.cjs --gallery      # module screenshots only
 *   node docs/website-assets/capture.cjs --flows        # workflow frames + WebM only
 *   node docs/website-assets/capture.cjs --only vigil   # scenarios whose name contains "vigil"
 *   ORB_THEMES=dark node ...                            # one theme
 *
 * Preconditions: backend on :8000 serving the built SPA (./run-orb.sh), demo DB freshly
 * reseeded (server/scripts/reset_db_and_seed.py), both models warm for the Sage flows.
 * Output: docs/website-assets/screenshots/<theme>/*.png (2x), frames/<flow>/*.png (1x),
 * video/<flow>.webm, screenshots/index.json, frames/<flow>/manifest.json.
 * Then run: server/.venv/bin/python docs/website-assets/build_assets.py
 */
const { chromium } = require('playwright')
const fs = require('fs')
const path = require('path')

const BASE = process.env.ORB_BASE_URL || 'http://localhost:8000'
const ROOT = __dirname
const SHOTS = process.env.ORB_SHOTS_DIR || path.join(ROOT, 'screenshots')
const FRAMES = process.env.ORB_FRAMES_DIR || path.join(ROOT, 'frames')
const VIDEO = process.env.ORB_VIDEO_DIR || path.join(ROOT, 'video')
const VIEWPORT = { width: 1512, height: 1024 }
const THEMES = (process.env.ORB_THEMES || 'dark,light').split(',').map(s => s.trim()).filter(Boolean)
const args = process.argv.slice(2)
const ONLY = (() => { const i = args.indexOf('--only'); return i >= 0 ? args[i + 1] : null })()
const DO_GALLERY = !args.includes('--flows')
const DO_FLOWS = !args.includes('--gallery')
const RECORD = args.includes('--record')
const FIXTURES = process.env.ORB_FIXTURES_DIR || path.join(ROOT, 'fixtures')
const recordings = {}   // persona -> { user, responses, frames }

/**
 * --record: while the scenarios run, capture every /api response and every
 * realtime frame the page receives, per persona. The website's demo build
 * replays them (demo/shim.ts in this repo), so the product runs there with
 * no backend. Streaming bodies are recorded whole and replayed event by event.
 */
function attachRecorder(page, persona, flow) {
  if (!RECORD || !persona) return
  const rec = recordings[persona] || (recordings[persona] = { persona, user: null, responses: [], frames: [], t0: Date.now() })
  if (flow) rec.flowStarts = { ...(rec.flowStarts || {}), [flow]: rec.responses.length }
  const flowKey = flow || null
  page.on('response', async res => {
    try {
      const u = new URL(res.url())
      if (!u.pathname.startsWith('/api/')) return
      const req = res.request()
      const ct = res.headers()['content-type'] || ''
      if (/image|octet-stream|pdf|zip/.test(ct)) return
      const body = await res.text().catch(() => '')
      const entry = { t: Date.now() - rec.t0, method: req.method(), url: u.pathname + u.search, status: res.status(), ct, body, req: req.method() === 'GET' ? undefined : (req.postData() || undefined), flow: flowKey || undefined }
      rec.responses.push(entry)
      if (u.pathname === '/api/auth/login' && res.status() === 200) {
        try { const j = JSON.parse(body); rec.user = j.user || j } catch {}
      }
    } catch {}
  })
  page.on('websocket', ws => {
    ws.on('framereceived', f => { try { rec.frames.push({ t: Date.now() - rec.t0, payload: String(f.payload), flow: flowKey || undefined }) } catch {} })
  })
}

const MERGE = args.includes('--merge')

function writeRecordings() {
  if (!RECORD) return
  fs.mkdirSync(FIXTURES, { recursive: true })
  for (const [persona, rec] of Object.entries(recordings)) {
    const { t0, ...rest } = rec
    const out = path.join(FIXTURES, `${persona}.json`)
    // recorded_at + t is the real clock time of every entry; the demo pins its clock to the end of the recording.
    let final = { ...rest, recorded_at: t0 }
    // --merge: append this run to the persona's existing recording (a re-run of one
    // flow with --only), shifting its clock past the end of what is already there.
    if (MERGE && fs.existsSync(out)) {
      try {
        const prev = JSON.parse(fs.readFileSync(out, 'utf8'))
        const last = Math.max(0, ...prev.responses.map(r => r.t), ...prev.frames.map(f => f.t)) + 1000
        // Appended entries keep their real clock time (recorded_at + t) when the earlier file has one.
        const shift = prev.recorded_at ? Math.max(last, t0 - prev.recorded_at) : last
        final = {
          persona,
          recorded_at: prev.recorded_at || t0,
          user: prev.user || rest.user,
          responses: [...prev.responses, ...rest.responses.map(r => ({ ...r, t: r.t + shift }))],
          frames: [...prev.frames, ...rest.frames.map(f => ({ ...f, t: f.t + shift }))],
        }
      } catch (e) { warn(`merge failed for ${persona}: ${e.message}`) }
    }
    fs.writeFileSync(out, JSON.stringify(final))
    log(`fixture ${persona}: ${final.responses.length} responses, ${final.frames.length} frames, ${(fs.statSync(out).size / 1e6).toFixed(1)} MB${MERGE ? ' (merged)' : ''}`)
    // One small fixture per workflow: the scripted demos on the website load only these.
    const flows = new Set(final.responses.map(r => r.flow).filter(Boolean))
    for (const flow of flows) {
      const fout = path.join(FIXTURES, `flow-${flow}.json`)
      const sub = { persona, flow, recorded_at: final.recorded_at, user: final.user, responses: final.responses.filter(r => r.flow === flow), frames: final.frames.filter(f => f.flow === flow) }
      fs.writeFileSync(fout, JSON.stringify(sub))
      log(`  flow-${flow}: ${sub.responses.length} responses, ${sub.frames.length} frames, ${(fs.statSync(fout).size / 1e3).toFixed(0)} KB`)
    }
  }
}
const LLM_TIMEOUT = 240_000

const CREDS = {
  doctor: { id: 'DOC001', password: 'password', mode: 'staff' },
  surgeon: { id: 'DOC004', password: 'password', mode: 'staff' },
  nurse: { id: 'NUR001', password: 'password', mode: 'staff' },
  pharmacist: { id: 'PHA001', password: 'password', mode: 'staff' },
  admin: { id: 'ADM001', password: 'password', mode: 'staff' },
  patient: { id: 'P001', password: 'patient123', mode: 'patient' },
}

const index = []       // gallery entries
const warnings = []
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a)
const warn = (m) => { warnings.push(m); console.warn('  ! ' + m) }
const sleep = (ms) => new Promise(r => setTimeout(r, ms))

fs.mkdirSync(SHOTS, { recursive: true })
fs.mkdirSync(FRAMES, { recursive: true })
fs.mkdirSync(VIDEO, { recursive: true })

async function newContext(browser, theme, opts = {}) {
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: opts.scale ?? 2,
    colorScheme: theme === 'light' ? 'light' : 'dark',
    reducedMotion: 'reduce',
    locale: 'en-IN',
    timezoneId: 'Asia/Kolkata',
    ...(opts.video ? { recordVideo: { dir: opts.video, size: VIEWPORT } } : {}),
  })
  await ctx.addInitScript((t) => {
    try { localStorage.setItem('orb-theme', t); localStorage.setItem('orb_theme', t) } catch {}
    // The capture machine has no Whisper runtime installed, so Scribe shows its
    // "transcription engine unavailable" notice. That is this laptop's install state,
    // not the product, so it is hidden in the captures (disclosed in the README).
    const css = '[data-testid="scribe-asr-unavailable"]{display:none !important}'
    const add = () => { try { const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s) } catch {} }
    if (document.head) add(); else document.addEventListener('DOMContentLoaded', add)
  }, theme)
  ctx.setDefaultTimeout(20_000)
  if (RECORD) ctx.on('page', p => attachRecorder(p, opts.persona, opts.flow))
  return ctx
}

async function login(page, who) {
  const c = CREDS[who]
  await page.goto(`${BASE}/login/${c.mode}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('#btn-login')
  if (c.mode === 'patient') { await page.fill('#input-registration', c.id); await page.fill('#input-dob', c.password) }
  else { await page.fill('#input-employee-id', c.id); await page.fill('#input-password', c.password) }
  const resp = page.waitForResponse(r => r.url().includes('/api/auth/login') && r.request().method() === 'POST', { timeout: 30_000 })
  await page.click('#btn-login')
  const r = await resp
  if (r.status() !== 200) throw new Error(`login ${c.id} -> HTTP ${r.status()}: ${await r.text().catch(() => '')}`)
  await page.waitForURL(c.mode === 'patient' ? '**/bridge**' : '**/dashboard**', { timeout: 30_000 })
  await settle(page, 900)
}

// The shell flips a surface to "Offline · saved copy, read-only" when one live fetch fails
// (a busy backend right after a reseed does it). Wait it out; on a fresh navigation, reload once.
async function waitOfflinePill(page, { reloadIfStuck = false } = {}) {
  const has = () => page.evaluate(() => /saved copy, read-only/.test(document.body.innerText || '')).catch(() => false)
  if (!(await has())) return
  const t0 = Date.now()
  while (Date.now() - t0 < 15000) { await sleep(500); if (!(await has())) return }
  warn('offline pill stayed up for 15 s' + (reloadIfStuck ? '; reloading the page' : ''))
  if (reloadIfStuck) { await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {}); await sleep(2000) }
}

async function settle(page, extra = 400) {
  await waitOfflinePill(page)
  await page.waitForLoadState('domcontentloaded').catch(() => {})
  await page.waitForLoadState('networkidle', { timeout: 3000 }).catch(() => {})
  await page.evaluate(async () => {
    try { await document.fonts.ready } catch {}
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))
  }).catch(() => {})
  // skeleton loaders and "Loading …" placeholders
  await page.waitForFunction(() => !document.querySelector('.nurse-skel__lead, .skeleton, [aria-busy="true"]'), null, { timeout: 2500 }).catch(() => {})
  await page.waitForFunction(() => !/\b(Loading|Fetching)\b[^\n]{0,60}(\.\.\.|…)/.test(document.body.innerText || ''), null, { timeout: 12_000 }).catch(() => {})
  await page.mouse.move(2, 2).catch(() => {})
  if (extra) await sleep(extra)
}

async function nav(page, tabId, waitSel) {
  await closeOverlays(page)
  await page.click(`#nav-${tabId}`)
  if (tabId === 'vigil') {
    // the Patients tab remembers the last opened chart; return to the board first
    const back = page.locator('.command-bar__back')
    if (await back.count()) { await back.first().click().catch(() => {}); await sleep(300) }
  }
  if (waitSel) {
    // the backend can starve its event loop for a few seconds after a burst of alerts
    // (Sage preloader); one retry of the click covers that without hiding a real failure
    try { await page.waitForSelector(waitSel, { timeout: 20_000 }) }
    catch (e) {
      await closeOverlays(page)
      await page.click(`#nav-${tabId}`).catch(() => {})
      await page.waitForSelector(waitSel, { timeout: 25_000 })
    }
  }
  await settle(page)
  await waitOfflinePill(page, { reloadIfStuck: true })
}

async function closeOverlays(page) {
  for (let i = 0; i < 3; i++) {
    await page.keyboard.press('Escape').catch(() => {})
    await sleep(150)
  }
  const cmdk = page.locator('.cmdk-overlay')
  if (await cmdk.count()) {
    await cmdk.first().click({ position: { x: 12, y: VIEWPORT.height - 12 }, timeout: 3000 }).catch(() => {})
    await page.waitForSelector('.cmdk-overlay', { state: 'detached', timeout: 3000 }).catch(() => {})
  }
  // The Pulse popover keeps a full-screen backdrop that swallows every click (Escape
  // does not close it); clicking the backdrop itself is the only way out.
  const backdrop = page.locator('.pulse-backdrop')
  if (await backdrop.count()) {
    // the sidebar sits above the backdrop, so click well inside the main content area
    await backdrop.first().click({ position: { x: 700, y: 700 }, timeout: 3000 }).catch(() => {})
    await page.waitForSelector('.pulse-backdrop', { state: 'detached', timeout: 4000 }).catch(() => {})
  }
  await settle(page, 200)
}

function shotter(theme, user) {
  const dir = path.join(SHOTS, theme)
  fs.mkdirSync(dir, { recursive: true })
  return async (page, name, title, opts = {}) => {
    const file = path.join(dir, `${name}.png`)
    await page.screenshot({ path: file, fullPage: !!opts.fullPage, animations: 'disabled', caret: 'hide' })
    index.push({ name, title, theme, user, file: path.relative(ROOT, file), fullPage: !!opts.fullPage })
    log(`  shot ${theme}/${name}.png`)
  }
}

async function patientIds(page) {
  return page.evaluate(async () => {
    const r = await fetch('/api/vigil/patients', { credentials: 'include' })
    if (!r.ok) return []
    const j = await r.json()
    const list = Array.isArray(j) ? j : (j.patients || j.items || [])
    return list.map(p => ({ id: p.id || p.patient_id, name: p.name }))
  }).catch(() => [])
}

async function waitForSageAnswer(page) {
  // The answer is complete when the meta/"Show details" disclosure renders on the last message.
  const t0 = Date.now()
  await page.waitForFunction(() => {
    // SagePanel renders the timing line (.sage-answer-meta--panel) as a sibling AFTER each assistant
    // message once streaming ends; an answer without a details disclosure has no marker inside the bubble.
    const msgs = document.querySelectorAll('.sage-msg, .sage-message, .sage-answer-meta--panel')
    const last = msgs[msgs.length - 1]
    if (!last) return false
    if (last.classList.contains('sage-answer-meta--panel')) return true
    const t = last.textContent || ''
    return /Show details|Hide details|first words|Answered by|could not|unavailable|not available/.test(t)
  }, null, { timeout: LLM_TIMEOUT }).catch(() => warn('Sage answer did not finish within the LLM timeout'))
  await sleep(800)
  log(`  sage answered in ${((Date.now() - t0) / 1000).toFixed(0)}s`)
}

// ───────────────────────────── gallery scenarios ─────────────────────────────

const GALLERY = [
  {
    name: 'public', user: null,
    run: async (page, shot) => {
      await page.goto(`${BASE}/login/staff`); await page.waitForSelector('#btn-login'); await settle(page, 800)
      await shot(page, 'login-staff', 'Staff sign-in')
      await page.goto(`${BASE}/login/patient`); await page.waitForSelector('#btn-login'); await settle(page, 800)
      await shot(page, 'login-patient', 'Patient portal sign-in')
    },
  },
  {
    name: 'doctor', user: 'doctor',
    run: async (page, shot) => {
      await page.waitForSelector('#panel-today, #panel-vigil').catch(() => {})
      await shot(page, 'today-doctor-home', 'Orb Today: the doctor\'s home screen')
      await nav(page, 'vigil', '.patient-row')
      await shot(page, 'vigil-patient-board', 'Vigil: live patient board with NEWS2 risk ordering')
      await page.locator('.patient-row', { hasText: 'Rajesh Iyer' }).first().click()
      await page.waitForSelector('.command-bar, .patient-detail'); await settle(page, 900)
      await shot(page, 'vigil-patient-chart', 'Vigil: patient chart with live vitals and NEWS2 band')
      const why = page.locator('.news2-drivers').first()
      if (await why.count()) { await why.scrollIntoViewIfNeeded().catch(() => {}); await settle(page, 500); await shot(page, 'vigil-news2-explained', 'Vigil: NEWS2 explained per parameter') }
      if (await page.locator('#btn-story').count()) {
        await page.click('#btn-story'); await page.waitForSelector('.story-modal'); await settle(page, 800)
        await shot(page, 'vigil-patient-story', 'Patient Story: one scrollable clinical narrative')
        await closeOverlays(page)
      }
      if (await page.locator('#btn-meds-reports').count()) {
        await page.click('#btn-meds-reports'); await page.waitForSelector('.modal-overlay'); await settle(page, 600)
        await shot(page, 'vigil-medications-emar', 'Medications and eMAR from the chart')
        await closeOverlays(page)
      }
      if (await page.locator('#btn-pulse').count()) {
        await page.click('#btn-pulse'); await page.waitForSelector('.pulse-panel', { timeout: 10_000 }).catch(() => {}); await settle(page, 600)
        await page.waitForFunction(() => !/Fetching environmental data/.test(document.querySelector('.pulse-panel')?.textContent || ''), null, { timeout: 15_000 }).catch(() => {})
        await settle(page, 400)
        await shot(page, 'pulse-environmental', 'Orb Pulse: environmental intelligence')
        await closeOverlays(page)
      }
      await nav(page, 'round', '#panel-round'); await shot(page, 'ward-round', 'Ward Round: the round as a queue')
      await nav(page, 'orders', '#panel-orders'); await shot(page, 'orders-cpoe', 'Orders (CPOE) with terminology validation')
      await page.click('#panel-orders button:has-text("Order sets")'); await page.waitForSelector('.order-sets'); await settle(page)
      await shot(page, 'orders-order-sets', 'Order sets: cited protocols behind the safety gates')
      const sepsis = page.locator('.order-sets__card', { hasText: /Sepsis Six/i }).first()
      if (await sepsis.count()) { await sepsis.click(); await settle(page, 700); await shot(page, 'orders-sepsis-six', 'Order sets: Sepsis Six, patient-specific gates') }
      await nav(page, 'relay', '.relay-channel-item'); await shot(page, 'relay-case-rooms', 'Relay: case rooms')
      const room = page.locator('.relay-channel-item', { hasText: /Suresh Reddy/ }).first()
      if (await room.count()) { await room.click(); await page.waitForSelector('.relay-chat__header'); await settle(page, 700); await shot(page, 'relay-case-room', 'Relay: a patient case room') }
      await nav(page, 'sage', '#panel-sage'); await shot(page, 'sage-panel', 'Sage: clinical assistant')
      if (RECORD) {
        // Record the four quick-action questions so the demo can answer each of them.
        for (let i = 0; i < 4; i++) {
          const nc = page.locator('button:has-text("New Chat")').first()
          if (await nc.count()) { await nc.click(); await settle(page, 500) }
          const card = page.locator('.sage-suggestion').nth(i)
          if (!(await card.count())) break
          await card.click(); await waitForSageAnswer(page); await settle(page, 500)
        }
      }
      await nav(page, 'scribe', '#panel-scribe'); await shot(page, 'scribe', 'Scribe: dictation to structured note')
      await nav(page, 'lens', '#panel-lens'); await shot(page, 'lens', 'Lens: imaging reference (investigational)')
      await nav(page, 'forecast', '#panel-forecast'); await shot(page, 'forecast-census', 'Forecast: census projection')
      for (const [label, name, title] of [
        ['Ward Capacity', 'forecast-capacity', 'Forecast: ward capacity'],
        ['Discharge Board', 'forecast-discharge-board', 'Forecast: discharge readiness'],
        ['Admission Patterns', 'forecast-admissions', 'Forecast: admission patterns'],
      ]) {
        const b = page.locator(`#panel-forecast button:has-text("${label}")`).first()
        if (await b.count()) { await b.click(); await settle(page, 700); await shot(page, name, title) }
      }
      for (const [tab, sel, name, title] of [
        ['command', '#panel-command', 'command-center', 'Command Center: hospital-wide view'],
        ['capacity', '#panel-capacity', 'capacity-simulator', 'What-if capacity simulator'],
        ['coding', '#panel-coding', 'revenue-integrity', 'Revenue integrity and coding'],
        ['appointments', '#panel-appointments', 'appointments', 'Appointments'],
        ['dietary', '#panel-dietary', 'dietary', 'Diet and kitchen'],
      ]) {
        if (await page.locator(`#nav-${tab}`).count()) { await nav(page, tab, sel); await settle(page, 500); await shot(page, name, title) }
        else warn(`doctor has no tab ${tab}`)
        if (RECORD && tab === 'capacity') {
          const presets = page.locator('.capsim__preset')
          const n = await presets.count()
          for (let i = 0; i < n; i++) { await presets.nth(i).click(); await settle(page, 900) }
        }
      }
      await nav(page, 'vigil', '.patient-row')
      const overlaySteps = [
        ['keyboard-shortcuts', 'Keyboard-first: shortcuts card', async () => { await page.keyboard.press('?'); await page.waitForSelector('.kbd-sheet', { timeout: 5000 }) }],
        ['command-palette', 'Command palette: find a patient', async () => { await page.keyboard.press('Meta+k'); await page.waitForSelector('.cmdk-panel', { timeout: 5000 }); await page.keyboard.type('Raj'); await sleep(700) }],
        ['admit-patient', 'Admit a patient', async () => { await page.click('#btn-admit-patient'); await page.waitForSelector('.admit-modal', { timeout: 8000 }) }],
        ['mfa-enrolment', 'Multi-factor authentication enrolment', async () => { await page.click('#btn-mfa-enroll'); await page.waitForSelector('.mfa-modal', { timeout: 8000 }) }],
      ]
      for (const [name, title, act] of overlaySteps) {
        try { await act(); await settle(page, 400); await shot(page, name, title) } catch (e) { warn(`${name}: ${e.message.split('\n')[0]}`) }
        await closeOverlays(page)
      }
      // standalone pages
      const ids = await patientIds(page)
      const rajesh = ids.find(p => /Rajesh Iyer/.test(p.name || '')) || ids[0]
      if (rajesh) {
        await page.goto(`${BASE}/downtime/${rajesh.id}`); await page.waitForSelector('.downtime-mode, .dt-toolbar', { timeout: 15_000 }).catch(() => {}); await settle(page, 900)
        await shot(page, 'downtime-mode', 'Downtime mode: the ward keeps a safe view when everything else is down', { fullPage: true })
        await page.goto(`${BASE}/handoff/${rajesh.id}`); await settle(page, 1200)
        await shot(page, 'handoff-ipass', 'I-PASS handoff document', { fullPage: true })
      } else warn('no patient id for downtime/handoff pages')
      // Record mode only, last so a hiccup here cannot skip anything above.
      if (RECORD) { try {
        await page.goto(`${BASE}/dashboard`); await page.waitForSelector('#nav-relay', { timeout: 15_000 })
        // An ordinary Relay message, so a typed message in the demo is not always the wrong-room one.
        await nav(page, 'relay', '.relay-channel-item')
        const room = page.locator('.relay-channel-item', { hasText: /Suresh Reddy/ }).first()
        if (await room.count()) {
          await room.click(); await page.waitForSelector('.relay-chat__header'); await settle(page, 500)
          await page.fill('input[placeholder="Type a message…"], textarea[placeholder="Type a message…"]', 'Started IV fluids at 14:20, will review at 16:00.')
          await page.keyboard.press('Enter'); await settle(page, 1500)
        }
            } catch (e) { warn('relay extra message: ' + e.message.split('\n')[0]) } }
    },
  },
  {
    name: 'surgeon', user: 'surgeon',
    run: async (page, shot) => {
      if (!(await page.locator('#nav-surgical-suite').count())) { warn('surgeon has no surgical-suite tab'); return }
      await nav(page, 'surgical-suite', '#panel-surgical-suite'); await settle(page, 600)
      await shot(page, 'surgical-suite-schedule', 'Surgical Suite: theatre schedule')
      const card = page.locator('.suite__day-card').first()
      if (await card.count()) { await card.click(); await settle(page, 800); await shot(page, 'surgical-suite-day', 'Surgical Suite: day view') }
      const back = page.locator('#suite-back-week'); if (await back.count()) { await back.click(); await settle(page, 500) }
      const active = page.locator('#panel-surgical-suite button:has-text("Active Cases")').first()
      if (await active.count()) { await active.click(); await settle(page, 800); await shot(page, 'surgical-suite-active-cases', 'Surgical Suite: active cases and WHO checklist') }
    },
  },
  {
    name: 'nurse', user: 'nurse',
    run: async (page, shot) => {
      await page.waitForSelector('#panel-shift'); await settle(page, 800)
      await shot(page, 'nurse-my-shift', 'Nurse mode: My Shift, what is due now')
      await page.click('#nurse-tab-patients').catch(() => warn('nurse-tab-patients missing')); await settle(page, 700)
      await shot(page, 'nurse-my-patients', 'Nurse mode: my patients')
      const obsBtn = page.locator('#panel-shift button:has-text("Record obs")').first()
      if (await obsBtn.count()) {
        await obsBtn.click(); await page.waitForSelector('#obs-sheet-title', { timeout: 10_000 }).catch(() => {}); await settle(page, 500)
        for (const [k, v] of [['respiratory_rate', '24'], ['spo2', '91'], ['systolic_bp', '98'], ['diastolic_bp', '60'], ['heart_rate', '112'], ['temperature', '38.4']]) {
          const f = page.locator(`#obs-${k}`); if (await f.count()) await f.fill(v)
        }
        await page.locator('.obs-segment__options button:has-text("On oxygen")').first().click().catch(() => {})
        await page.locator('.obs-segment__options button:has-text("Alert")').first().click().catch(() => {})
        await settle(page, 600)
        await shot(page, 'nurse-observations-news2-live', 'Nurse mode: observations with NEWS2 computed live')
        await page.locator('[aria-label="Close observation entry"]').click().catch(() => {})
        await sleep(300)
        await page.locator('button:has-text("Discard")').last().click().catch(() => {})
        await settle(page, 400)
      } else warn('no Record observations button found')
      await page.click('#nurse-tab-tasks').catch(() => {}); await settle(page, 500)
      const medRow = page.locator('#panel-shift .orb-row', { hasText: /mg|mcg|units|tab|ml/i }).first()
      if (await medRow.count()) {
        await medRow.click(); await page.waitForSelector('.administer-modal', { timeout: 8000 }).catch(() => {}); await settle(page, 500)
        if (await page.locator('.administer-modal').count()) await shot(page, 'nurse-administer-medication', 'Nurse mode: administer with the five rights and type-to-confirm')
        await closeOverlays(page)
      }
      await page.click('#nurse-tab-handover').catch(() => {}); await settle(page, 800)
      await shot(page, 'nurse-handover-ipass', 'Nurse mode: I-PASS handover draft')
      await page.click('#nurse-tab-shift').catch(() => {}); await settle(page, 800)
      await shot(page, 'nurse-end-of-shift', 'Nurse mode: end-of-shift outcome capture')
    },
  },
  {
    name: 'pharmacist', user: 'pharmacist',
    run: async (page, shot) => {
      await page.waitForSelector('#panel-helix'); await settle(page, 800)
      await shot(page, 'pharmacy-helix-overview', 'Helix: pharmacy overview')
      for (const [label, name, title] of [['Patient Board', 'pharmacy-patient-board', 'Helix: patient board'], ['Formulary', 'pharmacy-formulary', 'Helix: formulary']]) {
        const b = page.locator(`#panel-helix button:has-text("${label}")`).first()
        if (await b.count()) { await b.click(); await settle(page, 700); await shot(page, name, title) }
      }
      await nav(page, 'pharmacy-verify', '#panel-pharmacy-verify'); await settle(page, 600)
      await shot(page, 'pharmacy-verify-queue', 'Pharmacist verify queue with drug-safety screening')
      const row = page.locator('#panel-pharmacy-verify table tbody tr').first()
      if (await row.count()) {
        await row.evaluate(el => el.click()); await page.waitForSelector('[role="dialog"]', { timeout: 8000 }).catch(() => {}); await settle(page, 500)
        if (await page.locator('[role="dialog"]').count()) await shot(page, 'pharmacy-order-review', 'Pharmacist order review')
        await closeOverlays(page)
      }
    },
  },
  {
    name: 'admin', user: 'admin',
    run: async (page, shot) => {
      await nav(page, 'admin', '#panel-admin'); await settle(page, 700)
      const tabs = await page.evaluate(() => Array.from(document.querySelectorAll('[id^="admin-tab-"]')).map(e => ({ id: e.id.replace('admin-tab-', ''), text: (e.textContent || '').trim() })))
      log(`  admin tabs: ${tabs.map(t => t.id).join(', ')}`)
      for (const t of tabs) {
        await page.click(`#admin-tab-${t.id}`).catch(() => warn(`admin tab ${t.id} not clickable`))
        await settle(page, 900)
        await shot(page, `admin-${t.id}`, `Admin: ${t.text}`)
      }
      for (const [tab, title] of [
        ['frontdesk', 'Front Desk: registration and OPD queue'], ['billing', 'Billing: charges, invoices, GST'],
        ['payments', 'Payments: receipts, refunds, counter sessions'], ['payer', 'Insurance and TPA claims'],
        ['procurement', 'Procurement and stores'], ['housekeeping', 'Housekeeping and bed turnaround'],
        ['nabh', 'NABH evidence and quality indicators'], ['abdm', 'ABDM and ABHA identity'],
        ['workforce', 'Workforce: duty roster and attendance'], ['biomedical', 'Biomedical equipment register'],
      ]) {
        if (!(await page.locator(`#nav-${tab}`).count())) { warn(`admin has no tab ${tab}`); continue }
        await nav(page, tab, `#panel-${tab}`); await settle(page, 900)
        await shot(page, `module-${tab}`, title)
        const subtabs = page.locator(`#panel-${tab} [role="tab"]`)
        const n = Math.min(await subtabs.count(), 3)
        for (let i = 1; i < n; i++) {
          const label = ((await subtabs.nth(i).textContent()) || '').trim()
          if (!label) continue
          await subtabs.nth(i).click().catch(() => {}); await settle(page, 900)
          await shot(page, `module-${tab}-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}`, `${title}: ${label}`)
        }
      }
    },
  },
  {
    name: 'patient', user: 'patient',
    run: async (page, shot) => {
      await page.waitForSelector('#bridge-logout', { timeout: 20_000 }).catch(() => {}); await settle(page, 900)
      await shot(page, 'bridge-patient-portal', 'Bridge: the patient portal', { fullPage: true })
      if (RECORD) {
        for (const q of ['What medications am I on?', 'Who is looking after me today?']) {
          await page.fill('#bridge-ask-input', q); await page.click('#bridge-ask-btn')
          await page.waitForFunction(() => { const b = document.querySelector('#bridge-ask-btn'); const a = document.querySelector('.bridge__answer'); return a && b && !b.disabled }, null, { timeout: LLM_TIMEOUT }).catch(() => warn('bridge extra answer did not finish'))
          await settle(page, 600)
        }
      }
      for (const [btn, name, title] of [['#bridge-btn-meds', 'bridge-medications', 'Bridge: my medications'], ['#bridge-btn-labs', 'bridge-labs', 'Bridge: my results'], ['#bridge-btn-docs', 'bridge-documents', 'Bridge: my documents']]) {
        if (!(await page.locator(btn).count())) continue
        await page.click(btn); await page.waitForSelector('.modal-overlay', { timeout: 8000 }).catch(() => {}); await settle(page, 500)
        await shot(page, name, title)
        await page.locator('.modal-overlay button:has-text("Close")').first().click().catch(() => closeOverlays(page)); await sleep(300)
      }
    },
  },
]

// ───────────────────────────── workflows ─────────────────────────────

const FLOWS = [
  {
    id: 'ward-is-alive', user: 'doctor', title: 'The ward watches itself',
    subtitle: 'Live vitals, NEWS2 ordering, and a chart that explains its own score',
    steps: [
      { caption: 'Sign in as a doctor. Orb Today lists what needs you first: deteriorating patients, new results, pending signatures.', act: async p => { await p.waitForSelector('#panel-today, #panel-vigil'); await settle(p, 800) } },
      { caption: 'Patients: the ward ordered by risk. Vitals move because a monitor feed is running; the order is NEWS2-derived, not hand-placed.', act: async p => { await nav(p, 'vigil', '.patient-row'); await settle(p, 800) } },
      { caption: 'Open a chart. The NEWS2 band is computed by Orb\'s deterministic engine, never by a model.', act: async p => { await p.locator('.patient-row', { hasText: 'Rajesh Iyer' }).first().click(); await p.waitForSelector('.command-bar, .patient-detail'); await settle(p, 900) } },
      { caption: 'Why this score? Every parameter\'s contribution is shown. Orb will not give you a number without showing where it came from.', act: async p => { const t = p.locator('.news2-drivers').first(); if (await t.count()) await t.scrollIntoViewIfNeeded().catch(() => {}); await settle(p, 600) } },
      { caption: 'Patient Story: alerts, notes, doses given, handovers and bundles in one scrollable narrative, with a bookmark for what is new since you last looked.', act: async p => { await p.click('#btn-story'); await p.waitForSelector('.story-modal'); await settle(p, 900) }, hold: 4200 },
    ],
  },
  {
    id: 'allergy-interlock', user: 'doctor', title: 'The allergy interlock',
    subtitle: 'A sepsis bundle is applied whole or not at all',
    steps: [
      { caption: 'Orders, then Order sets: cited protocols. Every item still goes through the allergy interlock, the dose guard and terminology validation.', act: async p => { await nav(p, 'orders', '#panel-orders'); await p.click('#panel-orders button:has-text("Order sets")'); await p.waitForSelector('.order-sets'); await settle(p, 700) } },
      { caption: 'Pick Sepsis Six. The gates are patient-specific, so there is nothing honest to show until Orb knows whose chart this is.', act: async p => { await p.locator('.order-sets__card', { hasText: /Sepsis Six/i }).first().click(); await settle(p, 800) } },
      { caption: 'Apply to Suresh Reddy, 55, community-acquired pneumonia with early sepsis.', act: async p => {
        const inp = p.locator('.order-sets input[placeholder*="Search by name"]').first()
        await inp.fill('Suresh'); await p.waitForSelector('.patient-picker__option', { timeout: 10_000 }); await sleep(300)
        await p.locator('.patient-picker__option', { hasText: /Suresh/ }).first().click()
        await p.waitForSelector('.order-sets__block-chip, .order-sets__ok-chip', { timeout: 30_000 }).catch(() => warn('order-set preview verdicts did not render'))
        await settle(p, 900)
      } },
      { caption: 'Eight items clear. Ceftriaxone is STOPPED: a documented anaphylaxis to IV ceftriaxone in 2023. The Apply button is disabled, because a half-applied sepsis bundle is its own emergency.', act: async p => { const chip = p.locator('.order-sets__block-chip').first(); if (await chip.count()) await chip.scrollIntoViewIfNeeded().catch(() => {}); await settle(p, 500) }, hold: 5000 },
      { caption: 'Same set, different patient: Ananya Kapoor has a moderate penicillin rash. Orb raises a cross-reactivity alert but does not block. Four patients, four different answers.', act: async p => {
        const inp = p.locator('.order-sets input[placeholder*="Search by name"]').first()
        await inp.fill(''); await inp.fill('Ananya'); await p.waitForSelector('.patient-picker__option', { timeout: 10_000 }); await sleep(300)
        await p.locator('.patient-picker__option', { hasText: /Ananya/ }).first().click()
        await p.waitForSelector('.order-sets__caution-chip, .order-sets__ok-chip', { timeout: 30_000 }).catch(() => {})
        await settle(p, 900)
      }, hold: 4500 },
    ],
  },
  {
    id: 'wrong-room-warning', user: 'doctor', title: 'The wrong-room warning',
    subtitle: 'Wrong patient, right ward: the most common serious error, caught without a modal',
    steps: [
      { caption: 'Messages: every patient has a case room. Open Suresh Reddy\'s.', act: async p => { await nav(p, 'relay', '.relay-channel-item'); await p.locator('.relay-channel-item', { hasText: /Suresh Reddy/ }).first().click(); await p.waitForSelector('.relay-chat__header'); await settle(p, 800) } },
      { caption: 'Type an order about a different patient into this room: "Give Rajesh Iyer 40mg prednisolone now."', act: async p => { await p.fill('input[placeholder="Type a message…"], textarea[placeholder="Type a message…"]', 'Give Rajesh Iyer 40mg prednisolone now.'); await settle(p, 400) } },
      { caption: 'Sent, not blocked. A nurse mid-emergency does not need a modal. Orb delivers it and says: this looks like it belongs to Rajesh Iyer, want me to move it?', act: async p => { await p.keyboard.press('Enter'); await p.waitForSelector('.room-fit', { timeout: 30_000 }).catch(() => warn('room-fit warning did not render')); await settle(p, 900) }, hold: 5000 },
    ],
  },
  {
    id: 'sage-ask', user: 'doctor', title: 'Ask Sage',
    subtitle: 'A local 27B model that cites what it read, on a box with no internet',
    steps: [
      { caption: 'Sage runs entirely on the appliance. No PHI leaves the box.', act: async p => { await nav(p, 'sage', '#panel-sage'); await settle(p, 700) } },
      { caption: 'Fast mode answers general questions with the small model. Patient, medication and dose questions still go to the careful model. That floor is not a toggle in the UI.', act: async p => { const t = p.locator('.sage-think-toggle--on').first(); if (await t.count()) await t.click(); await settle(p, 700) } },
      { caption: 'Ask: what are the classic ECG findings in hyperkalaemia?', act: async p => { await p.fill('.sage-composer__input', 'What are the classic ECG findings in hyperkalaemia?'); await settle(p, 300) } },
      { caption: 'The answer, with a safety line inline and the guidelines it retrieved listed under "Show details".', act: async p => { await p.keyboard.press('Enter'); await waitForSageAnswer(p); const d = p.locator('button:has-text("Show details")').last(); if (await d.count()) { await d.click(); await settle(p, 700) } }, hold: 6000 },
    ],
  },
  {
    id: 'sage-refusal', user: 'doctor', title: 'The cross-department refusal',
    subtitle: 'A real doctor with a real login, and Orb still says no',
    steps: [
      { caption: 'New chat. This login is General Medicine; cardiology is not its ward.', act: async p => { await nav(p, 'sage', '#panel-sage'); const nc = p.locator('button:has-text("New Chat")').first(); if (await nc.count()) await nc.click(); await settle(p, 600) } },
      { caption: 'Ask for the potassium result of the patient in cardiology bed C-201.', act: async p => { await p.fill('.sage-composer__input', 'What is the potassium result for the patient in cardiology bed C-201?'); await settle(p, 300) } },
      { caption: 'No result. The cardiology chart is outside this login\'s scope, so Sage is never shown it. Scoping is enforced in the server, not hidden in the UI: a direct API call for that chart returns 404, as if the patient were not there at all.', act: async p => { await p.keyboard.press('Enter'); await waitForSageAnswer(p); await settle(p, 700) }, hold: 6000 },
    ],
  },
  {
    id: 'nurse-shift', user: 'nurse', title: 'The nurse\'s shift',
    subtitle: 'Same database, a different product: a list of what is due, and a score you see before you save',
    steps: [
      { caption: 'A nurse lands on My Shift, not on a chart: medications and observations due now, each with how late it is.', act: async p => { await p.waitForSelector('#panel-shift'); await settle(p, 900) } },
      { caption: 'My patients: the ward as this nurse owns it.', act: async p => { await p.click('#nurse-tab-patients'); await settle(p, 800) } },
      { caption: 'Record observations: big numeric targets built for a tablet on a trolley.', act: async p => { await p.locator('#panel-shift button:has-text("Record obs")').first().click(); await p.waitForSelector('#obs-sheet-title', { timeout: 10_000 }).catch(() => {}); await settle(p, 600) } },
      { caption: 'NEWS2 computes live as the values go in, with the RCP escalation sentence underneath. She sees what she is about to trigger before she saves it.', act: async p => {
        for (const [k, v] of [['respiratory_rate', '24'], ['spo2', '91'], ['systolic_bp', '98'], ['diastolic_bp', '60'], ['heart_rate', '112'], ['temperature', '38.4']]) { const f = p.locator(`#obs-${k}`); if (await f.count()) { await f.fill(v); await sleep(120) } }
        await p.locator('.obs-segment__options button:has-text("On oxygen")').first().click().catch(() => {})
        await p.locator('.obs-segment__options button:has-text("Alert")').first().click().catch(() => {})
        await settle(p, 800)
      }, hold: 5000 },
      { caption: 'Handover: an I-PASS draft written from recorded data, to be read against the patient before handing over.', act: async p => {
        await p.locator('[aria-label="Close observation entry"]').click().catch(() => {}); await sleep(300)
        await p.locator('button:has-text("Discard")').last().click().catch(() => {}); await sleep(300)
        await p.click('#nurse-tab-handover'); await settle(p, 900)
      }, hold: 4500 },
    ],
  },
  {
    id: 'show-your-work', user: 'admin', title: 'Show your work',
    subtitle: 'Live compliance posture, a tamper-evident audit chain, and an honest model register',
    steps: [
      { caption: 'Trust Center: compliance posture aggregated from the running system, not a PDF. Amber is amber; an all-green screen should be trusted less.', act: async p => { await nav(p, 'admin', '#panel-admin'); await p.click('#admin-tab-trust').catch(() => {}); await settle(p, 1200) } },
      { caption: 'Flight Recorder: a SHA-256 hash chain over every audit row. Alter or delete one and the chain breaks. Every question Sage was asked is in there, and every refusal.', act: async p => { await p.click('#admin-tab-flight').catch(() => {}); await settle(p, 1200) } },
      { caption: 'Model Governance: each model, its version, and whether it has been validated. The forecast model says "untrained". The validated one is the deterministic score that drives escalation.', act: async p => { await p.click('#admin-tab-models').catch(() => {}); await settle(p, 1000) }, hold: 5000 },
      { caption: 'Pilot Scorecard: how the pilot will be measured. Shadow-mode episodes, adjudication, outcomes, denominators included.', act: async p => { const t = p.locator('[id^="admin-tab-"]', { hasText: /Scorecard|Pilot/i }).first(); if (await t.count()) await t.click(); await settle(p, 1200) }, hold: 4500 },
    ],
  },
  {
    id: 'scribe-note', user: 'doctor', title: 'Scribe',
    subtitle: 'Dictation to a structured, verified note, on-device',
    steps: [
      { caption: 'Scribe: dictate or type. Nothing is sent to a cloud transcription service.', act: async p => { await nav(p, 'scribe', '#panel-scribe'); await settle(p, 700) } },
      { caption: 'Pick the patient, then the free-text consultation goes in.', act: async p => {
        // the patient/format controls only render once there is a transcript, so type first, then link the patient
        const pickPatient = async () => {
          const sel = p.locator('[data-testid="scribe-patient-select"]')
          await sel.waitFor({ timeout: 10_000 })
          await p.waitForFunction(() => Array.from(document.querySelectorAll('[data-testid="scribe-patient-select"] option')).some(o => /Rajesh Iyer/.test(o.textContent || '')), null, { timeout: 10_000 })
          const value = await sel.locator('option', { hasText: /Rajesh Iyer/ }).first().getAttribute('value')
          if (value) await sel.selectOption(value)
        }
        p.__pickPatient = pickPatient
        await p.fill('#scribe-input', 'Sixty-seven year old man, day three of a COPD exacerbation. Still breathless on minimal exertion, saturations ninety-one percent on two litres, respiratory rate twenty-four, afebrile, chest with scattered wheeze and prolonged expiration. Continue nebulised salbutamol and ipratropium, prednisolone forty milligrams once daily for five days, oxygen to target eighty-eight to ninety-two percent, repeat blood gas this evening, physiotherapy review, and reassess for discharge in forty-eight hours if improving.')
        await p.__pickPatient().catch(e => warn('patient link failed: ' + e.message.split('\n')[0]))
        await settle(p, 400)
      } },
      { caption: 'Structure Note: the local model drafts a SOAP note, then a second pass checks it against the transcript for unsupported claims and omissions before anyone signs it.', act: async p => {
        await p.locator('button:has-text("Structure Note")').first().click()
        await p.waitForFunction(() => { const t = document.body.textContent || ''; return /Note structured|Structuring failed/.test(t) && !/Processing\.\.\./.test(t) && !document.querySelector('.scribe-cursor') }, null, { timeout: LLM_TIMEOUT }).catch(() => warn('structured note did not appear in time'))
        // the verification pass (second model call) gates the Sign button; give it a bounded wait
        await p.waitForFunction(() => /Verification Findings|Verified|verified|No issues|Sign & Finalize|Sign\b/.test(document.body.textContent || ''), null, { timeout: 90_000 }).catch(() => warn('verification did not complete in time'))
        await settle(p, 1200)
      }, hold: 6000 },
      { caption: 'Sign & Finalize. This note was typed, never verified against audio, so Orb stops and makes the clinician type SIGN: accepting an unverified AI note is recorded in their own name.', act: async p => {
        const sign = p.locator('button', { hasText: /^[^A-Za-z]*Sign(\s*&|\s+and\b|\s+Note|\s*$)/ }).first()
        await sign.waitFor({ timeout: 15_000 }).catch(() => warn('Sign button not found'))
        if (await sign.count()) await sign.click()
        await Promise.race([
          p.locator('#scribe-unverified-confirm').waitFor({ timeout: 8000 }),
          p.waitForFunction(() => /Signed and finalized/.test(document.body.textContent || ''), null, { timeout: 8000 }),
        ]).catch(() => warn('neither the SIGN confirm nor the signed state appeared'))
        await settle(p, 600)
      }, hold: 4500 },
      { caption: 'Signed. Signing is the trigger: medications named in the note are extracted, screened, and queued for a pharmacist before they become active orders.', act: async p => {
        const confirm = p.locator('#scribe-unverified-confirm')
        if (await confirm.count()) {
          const label = (await p.locator('label[for="scribe-unverified-confirm"]').textContent().catch(() => '')) || ''
          const ph = (await confirm.getAttribute('placeholder').catch(() => '')) || ''
          const phrase = (label.match(/\b([A-Z]{3,})\b/) || ph.match(/["“]([^"”]+)["”]/) || [])[1] || 'SIGN'
          await confirm.fill(phrase)
          await p.locator('button:has-text("Sign anyway")').first().click().catch(() => warn('Sign anyway click failed'))
        }
        await p.waitForFunction(() => /Signed and finalized/.test(document.body.textContent || ''), null, { timeout: 45_000 }).catch(() => warn('note did not reach the signed state'))
        await settle(p, 1200)
      }, hold: 5000 },
    ],
  },
  {
    id: 'pharmacy-verify', user: 'pharmacist', title: 'Pharmacist verification',
    subtitle: 'Nothing AI-extracted is given until a pharmacist has verified it',
    steps: [
      { caption: 'Helix: the pharmacy overview.', act: async p => { await p.waitForSelector('#panel-helix'); await settle(p, 900) } },
      { caption: 'The verify queue: medications extracted from signed notes wait here for a pharmacist before they become active orders. Each is run against the interaction knowledge base; when a drug is outside it, Orb says NOT SCREENED instead of pretending.', act: async p => {
        await nav(p, 'pharmacy-verify', '#panel-pharmacy-verify')
        for (let i = 0; i < 12; i++) {
          if (await p.locator('#panel-pharmacy-verify table tbody tr').count()) break
          await p.locator('#panel-pharmacy-verify button:has-text("Refresh")').first().click().catch(() => {})
          await sleep(8000)
        }
        await settle(p, 800)
      } },
      { caption: 'One row per extracted medication: dose, route and frequency editable, the source marked AI EXTRACTED, the safety line in plain words. Verify or Reject, and either decision is on the record in the pharmacist\'s name.', act: async p => { const row = p.locator('#panel-pharmacy-verify table tbody tr').first(); if (await row.count()) { await row.evaluate(el => el.click()); await p.waitForSelector('[role="dialog"]', { timeout: 8000 }).catch(() => {}) } await settle(p, 800) }, hold: 4500 },
    ],
  },
  {
    id: 'patient-portal', user: 'patient', title: 'Bridge, the patient portal',
    subtitle: 'The patient sees their own care in plain language',
    steps: [
      { caption: 'Bridge: care team, medications, results and documents for the patient, with a one-time login issued at admission.', act: async p => { await p.waitForSelector('#bridge-logout', { timeout: 20_000 }).catch(() => {}); await settle(p, 900) } },
      { caption: 'Ask a question in plain language. The answer is scoped to this patient and never gives medical advice; it points back to the nurse or doctor.', act: async p => { await p.fill('#bridge-ask-input', 'When can I go home?'); await p.click('#bridge-ask-btn'); await p.waitForSelector('.bridge__answer', { timeout: LLM_TIMEOUT }).catch(() => warn('bridge answer did not render')); await p.waitForFunction(() => { const b = document.querySelector('#bridge-ask-btn'); const a = document.querySelector('.bridge__answer'); return a && b && !b.disabled }, null, { timeout: LLM_TIMEOUT }).catch(() => warn('bridge answer did not finish streaming')); await p.waitForFunction(() => { const t = document.querySelector('.bridge__answer-text')?.textContent || ''; if (window.__bt === t) return true; window.__bt = t; return false }, null, { timeout: 30_000, polling: 1500 }).catch(() => {}); await p.locator('.bridge__answer').first().scrollIntoViewIfNeeded().catch(() => {}); await sleep(400); await p.evaluate(() => document.querySelector('.bridge__answer')?.scrollIntoView({ block: 'center' })).catch(() => {}); await settle(p, 900) }, hold: 5000 },
    ],
  },
]

// ───────────────────────────── runner ─────────────────────────────

async function runGallery(browser) {
  for (const theme of THEMES) {
    for (const sc of GALLERY) {
      if (ONLY && !sc.name.includes(ONLY)) continue
      log(`gallery ${theme}/${sc.name}`)
      const ctx = await newContext(browser, theme, { persona: sc.user })
      const page = await ctx.newPage()
      const shot = shotter(theme, sc.user)
      try {
        if (sc.user) await login(page, sc.user)
        await sc.run(page, shot)
      } catch (e) {
        warn(`gallery ${theme}/${sc.name} failed: ${e.message.split('\n')[0]}`)
        await page.screenshot({ path: path.join(SHOTS, theme, `_error-${sc.name}.png`) }).catch(() => {})
      }
      await ctx.close()
    }
  }
  fs.writeFileSync(path.join(SHOTS, 'index.json'), JSON.stringify(index, null, 2))
}

async function runFlows(browser) {
  const theme = process.env.ORB_FLOW_THEME || 'dark'
  for (const flow of FLOWS) {
    if (ONLY && !flow.id.includes(ONLY)) continue
    log(`flow ${flow.id}`)
    const dir = path.join(FRAMES, flow.id)
    fs.rmSync(dir, { recursive: true, force: true }); fs.mkdirSync(dir, { recursive: true })
    const vdir = path.join(VIDEO, `_tmp-${flow.id}`)
    fs.rmSync(vdir, { recursive: true, force: true })
    const ctx = await newContext(browser, theme, { scale: 1, video: vdir, persona: flow.user, flow: flow.id })
    const page = await ctx.newPage()
    const manifest = { id: flow.id, title: flow.title, subtitle: flow.subtitle, user: CREDS[flow.user].id, theme, viewport: VIEWPORT, steps: [] }
    try {
      await login(page, flow.user)
      let n = 0
      for (const step of flow.steps) {
        n += 1
        try { await step.act(page) } catch (e) { warn(`flow ${flow.id} step ${n} failed: ${e.message.split('\n')[0]}`) }
        const file = `${String(n).padStart(2, '0')}.png`
        await page.screenshot({ path: path.join(dir, file), animations: 'disabled', caret: 'hide' })
        manifest.steps.push({ n, caption: step.caption, file, hold: step.hold || 3200 })
        log(`  frame ${flow.id}/${file}`)
        await sleep(step.hold ? Math.min(step.hold, 2500) : 1200) // dwell for the video
      }
    } catch (e) {
      warn(`flow ${flow.id} failed: ${e.message.split('\n')[0]}`)
    }
    const video = page.video()
    await ctx.close()
    try {
      const vp = await video.path()
      fs.renameSync(vp, path.join(VIDEO, `${flow.id}.webm`))
      fs.rmSync(vdir, { recursive: true, force: true })
      manifest.video = `video/${flow.id}.webm`
    } catch (e) { warn(`no video for ${flow.id}: ${e.message}`) }
    fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(manifest, null, 2))
  }
}

;(async () => {
  const health = await fetch(`${BASE}/api/health`).then(r => r.status).catch(() => 0)
  if (health !== 200) { console.error(`backend not healthy at ${BASE} (HTTP ${health})`); process.exit(2) }
  const browser = await chromium.launch()
  try {
    if (DO_FLOWS) await runFlows(browser)
    if (DO_GALLERY) await runGallery(browser)
  } finally {
    await browser.close()
  }
  if (warnings.length) { console.log('\nWarnings:'); for (const w of warnings) console.log(' - ' + w) }
  writeRecordings()
  log('done')
})()
