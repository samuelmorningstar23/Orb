/**
 * Overnight QA screenshot sweep #2 — surfaces the first sweep did not reach.
 *
 * Same contract as scripts/qa-screenshots.mjs: drives the BUILT SPA on :8000
 * with Playwright and writes one PNG per screen into
 * docs/screenshots/2026-08-12-overnight/, prefixed `sweep2-`. Every screen here
 * was walked by hand in a real browser first; this only re-captures them so the
 * founder can see what the report describes.
 *
 *   node scripts/qa-screenshots-sweep2.mjs
 *
 * Not a test: it asserts nothing and never fails the build. A screen that will
 * not load is captured as it actually looks.
 */
import { chromium, devices } from 'playwright'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

const BASE = process.env.ORB_QA_BASE ?? 'http://localhost:8000'
const OUT = path.resolve('docs/screenshots/2026-08-12-overnight')
const VIEWPORT = { width: 1440, height: 900 }

async function shot(page, name) {
  await page.waitForTimeout(1200)
  await page.screenshot({ path: path.join(OUT, `sweep2-${name}.png`), fullPage: true })
  console.log('  ✓ sweep2-' + name)
}

async function clearSW(page) {
  // A stale Workbox service worker will happily serve last week's bundle.
  await page.evaluate(async () => {
    const regs = await navigator.serviceWorker.getRegistrations()
    await Promise.all(regs.map(r => r.unregister()))
    const keys = await caches.keys()
    await Promise.all(keys.map(k => caches.delete(k)))
  }).catch(() => {})
}

async function login(page, id, password) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await clearSW(page)
  await page.reload({ waitUntil: 'domcontentloaded' })
  const idField = page.locator('#employeeId, input[placeholder="DOC001"]').first()
  await idField.waitFor({ timeout: 20000 })
  await idField.fill(id)
  await page.locator('input[type="password"]').first().fill(password)
  await page.locator('button[type="submit"]').first().click()
  await page.waitForTimeout(4000)
}

/** Click a left-rail tab by its visible label. The rail scrolls: at 900px tall
 *  the last entries sit below the fold, so scroll the item into view first. */
async function openRailTab(page, label) {
  const ok = await page.evaluate(l => {
    const el = [...document.querySelectorAll('.shell__nav-item')]
      .find(i => i.textContent.trim() === l)
    if (!el) return false
    el.scrollIntoView({ block: 'center' })
    el.click()
    return true
  }, label)
  if (ok) await page.waitForTimeout(1800)
  else console.log('  – rail tab absent:', label)
  return ok
}

/** A button inside the panel body, matched on its leading text. */
async function clickText(page, text, waitMs = 1800) {
  const ok = await page.evaluate(t => {
    const el = [...document.querySelectorAll('button')]
      .find(b => b.textContent.trim() === t || b.textContent.trim().startsWith(t))
    if (!el) return false
    el.scrollIntoView({ block: 'center' })
    el.click()
    return true
  }, text)
  if (ok) await page.waitForTimeout(waitMs)
  else console.log('  – button absent:', text)
  return ok
}

async function main() {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch()

  // ── 1. Pharmacist (PHA001) — the rail nobody had opened ──────────────────
  {
    const ctx = await browser.newContext({ viewport: VIEWPORT })
    const page = await ctx.newPage()
    console.log('PHA001 — pharmacist rail')
    await login(page, 'PHA001', 'password')
    await shot(page, 'pharmacist-helix-overview')
    await clickText(page, 'Patient Board'); await shot(page, 'pharmacist-helix-patient-board')
    await clickText(page, 'Formulary');     await shot(page, 'pharmacist-helix-formulary')
    await openRailTab(page, 'Verify Queue'); await shot(page, 'pharmacist-verify-queue')
    await openRailTab(page, 'Sage');         await shot(page, 'pharmacist-sage')
    await ctx.close()
  }

  // ── 2. Admin (ADM001) — all 15 governance sub-tabs ───────────────────────
  {
    const ctx = await browser.newContext({ viewport: VIEWPORT })
    const page = await ctx.newPage()
    console.log('ADM001 — admin sub-tabs')
    await login(page, 'ADM001', 'password')
    await openRailTab(page, 'Admin')
    const subTabs = [
      ['Overview', 'admin-overview'],
      ['Pilot Scorecard', 'admin-pilot-scorecard'],
      ['Audit Logs', 'admin-audit-logs'],
      ['Trace', 'admin-trace'],
      ['Sage Provenance', 'admin-sage-provenance'],
      ['Ingestion', 'admin-ingestion'],
      ['Security', 'admin-security'],
      ['Alerts', 'admin-alerts'],
      ['Model Governance', 'admin-model-governance'],
      ['Impact', 'admin-impact'],
      ['Trust Center', 'admin-trust-center'],
      ['Flight Recorder', 'admin-flight-recorder'],
      ['Users', 'admin-users'],
      ['Roster Import', 'admin-roster-import'],
      ['Data & Backups', 'admin-data-backups'],
    ]
    for (const [label, name] of subTabs) {
      const ok = await page.evaluate(l => {
        const el = [...document.querySelectorAll('.admin-tab')]
          .find(t => t.textContent.trim() === l)
        if (!el) return false
        el.click(); return true
      }, label)
      if (!ok) { console.log('  – sub-tab absent:', label); continue }
      // Flight Recorder verifies the whole hash chain before it paints.
      await page.waitForTimeout(label === 'Flight Recorder' ? 7000 : 2600)
      await shot(page, name)
    }

    // ── 3. Hospital modules, second-level screens ──────────────────────────
    console.log('ADM001 — hospital modules')
    for (const [tab, name] of [
      ['Front Desk', 'module-frontdesk-opd-queue'],
      ['Billing', 'module-billing-bills'],
      ['Payments', 'module-payments'],
      ['Insurance', 'module-payer-claims'],
      ['Procurement', 'module-procurement'],
      ['Housekeeping', 'module-housekeeping'],
      ['Accreditation', 'module-nabh-incidents'],
      ['ABDM', 'module-abdm'],
      ['Workforce', 'module-workforce'],
      ['Equipment', 'module-biomedical'],
      ['Diet & Kitchen', 'module-dietary'],
    ]) {
      if (await openRailTab(page, tab)) await shot(page, name)
    }

    // Front Desk → Transfers, and the transfer dialog itself.
    if (await openRailTab(page, 'Front Desk')) {
      await clickText(page, 'Transfers'); await shot(page, 'module-frontdesk-transfers')
      const opened = await page.evaluate(() => {
        const row = [...document.querySelectorAll('tr')].find(r => /Rajesh Iyer/.test(r.innerText))
        if (!row) return false
        row.querySelector('button')?.click(); return true
      })
      if (opened) { await page.waitForTimeout(1800); await shot(page, 'module-frontdesk-transfer-modal') }
      await page.keyboard.press('Escape').catch(() => {})
    }

    // Billing → Charges (the tab that drops its patient), and an issued invoice.
    if (await openRailTab(page, 'Billing')) {
      await clickText(page, 'Charges'); await shot(page, 'module-billing-charges-empty')
      await page.evaluate(() => {
        const sel = document.querySelector('#charges-patient')
        const opt = [...sel.options].find(o => /Ananya/.test(o.text))
        if (!opt) return
        Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value')
          .set.call(sel, opt.value)
        sel.dispatchEvent(new Event('change', { bubbles: true }))
      })
      await page.waitForTimeout(2600)
      await shot(page, 'module-billing-charges-patient-loaded')
      await clickText(page, 'Bills')
      await clickText(page, 'Charges')
      await shot(page, 'module-billing-charges-patient-LOST-after-roundtrip')
      await clickText(page, 'Bills')
      const opened = await page.evaluate(() => {
        const row = [...document.querySelectorAll('tr')].find(r => /INV-2026/.test(r.innerText))
        if (!row) return false
        row.click(); return true
      })
      if (opened) { await page.waitForTimeout(2200); await shot(page, 'module-billing-invoice-credit-note') }
    }
    await ctx.close()
  }

  // ── 4. Standalone clinician pages ────────────────────────────────────────
  {
    const ctx = await browser.newContext({ viewport: VIEWPORT })
    const page = await ctx.newPage()
    console.log('DOC001 — handoff / downtime / discharge / scribe')
    await login(page, 'DOC001', 'password')
    await page.goto(`${BASE}/handoff/PAT-F5463C3E`, { waitUntil: 'domcontentloaded' })
    await shot(page, 'page-handoff')
    await page.goto(`${BASE}/downtime/PAT-F5463C3E`, { waitUntil: 'domcontentloaded' })
    await shot(page, 'page-downtime')
    await page.goto(`${BASE}/discharge/qa-sweep2-invalid-token`, { waitUntil: 'domcontentloaded' })
    await shot(page, 'page-discharge-access-dob-gate')
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    await openRailTab(page, 'Scribe')
    await shot(page, 'doctor-scribe-asr-degraded-banner')
    await ctx.close()
  }

  // ── 5. Tablet + coarse pointer — the two touch-target fixes ──────────────
  {
    const ctx = await browser.newContext({ ...devices['iPad (gen 7)'] })
    const page = await ctx.newPage()
    console.log('tablet/touch — rail labels + chart command bar')
    await login(page, 'DOC001', 'password')
    await shot(page, 'tablet-touch-rail-labels-fixed-0_7rem')
    await openRailTab(page, 'Patients')
    await page.evaluate(() => document.querySelector('.patient-row')?.click())
    await page.waitForTimeout(6000)
    await shot(page, 'tablet-touch-chart-command-bar-fixed-44px')
    await ctx.close()
  }

  await browser.close()
  console.log('\nDone →', OUT)
}

main().catch(e => { console.error('sweep2 screenshots failed:', e); process.exit(0) })
