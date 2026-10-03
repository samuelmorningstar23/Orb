/**
 * Overnight QA — the screens the main sweep cannot reach.
 *
 *   node scripts/qa-screenshots-extra.mjs
 *
 * Three things the role sweep misses:
 *   1. a COARSE-POINTER tablet. The main sweep resizes the viewport, which
 *      changes width media queries but leaves `pointer: fine` — so the 44px tap
 *      floor and the rail's tablet labels never engage. This context emulates a
 *      real touch device.
 *   2. the Surgical Suite, which only a doctor in a surgery department sees
 *      (DOC004 · General Surgery), and the WHO checklist two clicks inside it.
 *   3. Scribe with the floating Sage orb lifted clear of its composer.
 */
import { chromium, devices } from 'playwright'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

const BASE = process.env.ORB_QA_BASE ?? 'http://localhost:8000'
const OUT = path.resolve('docs/screenshots/2026-08-12-overnight')

const shot = async (page, name) => {
  await page.waitForTimeout(1400)
  await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true })
  console.log('  ✓', name)
}

async function login(page, id, password) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.evaluate(async () => {
    const regs = await navigator.serviceWorker.getRegistrations()
    await Promise.all(regs.map(r => r.unregister()))
    const keys = await caches.keys()
    await Promise.all(keys.map(k => caches.delete(k)))
  })
  await page.reload({ waitUntil: 'domcontentloaded' })
  const idField = page.locator('input[placeholder="DOC001"]').first()
  await idField.waitFor({ timeout: 15000 })
  await idField.fill(id)
  await page.locator('input[type="password"]').first().fill(password)
  await page.locator('button[type="submit"]').first().click()
  await page.waitForTimeout(3500)
}

const run = async () => {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch()

  console.log('tablet with a real touch pointer — DOC001')
  const touch = await browser.newContext({
    ...devices['iPad (gen 7) landscape'],
    deviceScaleFactor: 2,
  })
  const tp = await touch.newPage()
  await login(tp, 'DOC001', 'password')
  await shot(tp, 'tablet-touch-doctor-today-rail-labels')
  const vigil = tp.locator('[role="tab"]', { hasText: /^Patients$/ }).first()
  if (await vigil.count()) { await vigil.tap(); await shot(tp, 'tablet-touch-vigil-board') }
  await touch.close()

  console.log('DOC004 — Surgical Suite + WHO checklist')
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await login(page, 'DOC004', 'password')
  const suite = page.locator('[role="tab"]', { hasText: /^Surgical Suite$/ }).first()
  if (await suite.count()) {
    await suite.click()
    await shot(page, 'surgeon-surgical-suite-schedule')
    const active = page.locator('button', { hasText: /^Active Cases/ }).first()
    if (await active.count()) { await active.click(); await shot(page, 'surgeon-surgical-suite-active-cases') }
    const sched = page.locator('button', { hasText: /^Schedule$/ }).first()
    if (await sched.count()) await sched.click()
    const surgery = page.locator('button', { hasText: /Exploratory Laparotomy/ }).first()
    if (await surgery.count()) {
      await surgery.click()
      await page.waitForTimeout(6000)
      await shot(page, 'surgeon-who-safety-checklist')
    }
  }

  console.log('Scribe — floating orb clear of the composer')
  const scribe = page.locator('[role="tab"]', { hasText: /^Scribe$/ }).first()
  if (await scribe.count()) { await scribe.click(); await shot(page, 'doctor-scribe-fab-clear') }

  await browser.close()
  console.log('\nWrote PNGs to', OUT)
}

run().catch(e => { console.error(e); process.exit(1) })
