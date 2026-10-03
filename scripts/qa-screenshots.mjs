/**
 * Overnight QA screenshot sweep — 2026-08-12.
 *
 * Drives the BUILT SPA on :8000 with Playwright and writes one PNG per screen
 * into docs/screenshots/2026-08-12-overnight/. It exists because the interactive
 * walkthrough cannot write files; every finding in the report was made by hand
 * in a real browser first and this only re-captures the same screens so the
 * founder can see them.
 *
 *   node scripts/qa-screenshots.mjs
 *
 * Not a test: it asserts nothing and never fails the build. A screen that will
 * not load is captured as it actually looks.
 */
import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

const BASE = process.env.ORB_QA_BASE ?? 'http://localhost:8000'
const OUT = path.resolve('docs/screenshots/2026-08-12-overnight')
const VIEWPORT = { width: 1440, height: 900 }

async function shot(page, name) {
  await page.waitForTimeout(1400)
  await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true })
  console.log('  ✓', name)
}

async function login(page, id, password) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  // A stale service worker will happily serve last week's bundle.
  await page.evaluate(async () => {
    const regs = await navigator.serviceWorker.getRegistrations()
    await Promise.all(regs.map(r => r.unregister()))
    const keys = await caches.keys()
    await Promise.all(keys.map(k => caches.delete(k)))
  })
  await page.reload({ waitUntil: 'domcontentloaded' })
  const idField = page.locator('#employeeId, input[placeholder="DOC001"]').first()
  await idField.waitFor({ timeout: 15000 })
  await idField.fill(id)
  await page.locator('input[type="password"]').first().fill(password)
  await page.locator('button[type="submit"]').first().click()
  await page.waitForTimeout(3500)
}

async function openTab(page, label) {
  const tab = page.locator(`[role="tab"]`, { hasText: new RegExp(`^${label}$`) }).first()
  if (await tab.count()) { await tab.click(); await page.waitForTimeout(1200); return true }
  return false
}

/** A tab-strip inside a panel. Never throws: a view that will not open is a
 *  finding for the report, not a reason to lose the rest of the sweep. */
async function subView(page, label, name) {
  try {
    const b = page.locator('button', { hasText: new RegExp(`^${label}`) }).first()
    if (!(await b.count())) { console.log('  – absent:', label); return }
    await b.click({ timeout: 5000 })
    await shot(page, name)
  } catch {
    console.log('  – could not open:', label)
  }
}

async function signOut(page) {
  const out = page.locator('button', { hasText: /^Sign ?[Oo]ut$/ }).first()
  if (await out.count()) { await out.click(); await page.waitForTimeout(1500) }
}

const DOCTOR = [
  ['Today', 'doctor-today-home'],
  ['Patients', 'doctor-vigil-board'],
  ['Ward Round', 'doctor-ward-round'],
  ['Orders', 'doctor-orders-cpoe'],
  ['Messages', 'doctor-relay-messages'],
  ['Sage', 'doctor-sage'],
  ['Scribe', 'doctor-scribe-degraded'],
  ['Lens', 'doctor-lens'],
  ['Forecast', 'doctor-forecast'],
  ['Command', 'doctor-command-center'],
  ['Simulator', 'doctor-what-if-simulator'],
  ['Revenue', 'doctor-revenue-integrity'],
  ['Appointments', 'doctor-appointments'],
  ['Diet & Kitchen', 'doctor-diet-kitchen'],
]

const NURSE = [
  ['My Shift', 'nurse-my-shift-due-now'],
  ['Patients', 'nurse-vigil-board'],
  ['Orders', 'nurse-orders'],
  ['Messages', 'nurse-relay-messages'],
  ['Diet & Kitchen', 'nurse-diet-kitchen'],
]

const ADMIN = [
  ['Patients', 'admin-vigil-board'],
  ['Command', 'admin-command-center'],
  ['Front Desk', 'admin-front-desk'],
  ['Billing', 'admin-billing'],
  ['Payments', 'admin-payments'],
  ['Insurance', 'admin-payer-claims'],
  ['Procurement', 'admin-procurement'],
  ['Housekeeping', 'admin-housekeeping'],
  ['Accreditation', 'admin-nabh-accreditation'],
  ['ABDM', 'admin-abdm'],
  ['Workforce', 'admin-workforce'],
  ['Equipment', 'admin-biomedical'],
  ['Diet & Kitchen', 'admin-diet-kitchen'],
  ['Admin', 'admin-overview'],
]

const run = async () => {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
  const page = await ctx.newPage()

  console.log('login screen')
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await shot(page, 'login')

  console.log('DOC001 — doctor')
  await login(page, 'DOC001', 'password')
  for (const [label, name] of DOCTOR) {
    if (await openTab(page, label)) await shot(page, name)
    else console.log('  – not reachable:', label)
  }
  // Sub-screens that need one more click.
  if (await openTab(page, 'Billing')) { /* not a doctor tab */ }
  await signOut(page)

  console.log('NUR001 — nurse')
  await login(page, 'NUR001', 'password')
  for (const [label, name] of NURSE) {
    if (await openTab(page, label)) await shot(page, name)
    else console.log('  – not reachable:', label)
  }
  // Back to the shift panel: the sub-views live inside it and the loop above
  // left the rail on another tab, where they are display:none rather than gone.
  await openTab(page, 'My Shift')
  for (const [view, name] of [['My patients', 'nurse-my-patients'], ['Handover', 'nurse-handover-ipass'], ['End of shift', 'nurse-end-of-shift']]) {
    await subView(page, view, name)
  }
  await signOut(page)

  console.log('ADM001 — administrator')
  await login(page, 'ADM001', 'password')
  for (const [label, name] of ADMIN) {
    if (await openTab(page, label)) await shot(page, name)
    else console.log('  – not reachable:', label)
  }
  for (const [view, name] of [
    ['Pilot Scorecard', 'admin-shadow-scorecard'],
    ['Model Governance', 'admin-model-governance'],
    ['Sage Provenance', 'admin-sage-provenance'],
    ['Users', 'admin-users'],
  ]) {
    await subView(page, view, name)
  }
  await signOut(page)

  console.log('P001 — Bridge patient portal')
  await login(page, 'P001', 'patient123')
  await shot(page, 'patient-bridge-portal')
  await signOut(page)

  console.log('themes + tablet — doctor Today')
  await login(page, 'DOC001', 'password')
  for (const theme of ['light', 'night', 'dark']) {
    await page.evaluate(t => {
      document.documentElement.setAttribute('data-theme', t)
      localStorage.setItem('orb-theme', t)
    }, theme)
    await shot(page, `theme-${theme}-doctor-today`)
  }
  await page.setViewportSize({ width: 834, height: 1112 })
  await page.waitForTimeout(800)
  await shot(page, 'tablet-834-doctor-today')
  if (await openTab(page, 'Patients')) await shot(page, 'tablet-834-vigil-board')

  await browser.close()
  console.log('\nWrote PNGs to', OUT)
}

run().catch(e => { console.error(e); process.exit(1) })
