/**
 * Capture the screenshots embedded in the user manual.
 *
 * Drives the REAL running app (localhost:8000) with real seeded data, rather
 * than the visual-audit harness's mocked fixtures, so the figures in the manual
 * match what a clinician actually sees. Signs in per role because several
 * screens are role-gated (Helix = pharmacist, Admin = admin, Bridge = patient).
 *
 *   node scripts/capture_manual_shots.mjs
 */
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import path from 'node:path'

const BASE = process.env.ORB_BASE_URL ?? 'http://localhost:8000'
const OUT = path.resolve(import.meta.dirname, '..', 'docs', 'assets', 'manual')
mkdirSync(OUT, { recursive: true })

// Clip to the viewport rather than fullPage: a 6000px-tall page scaled into an
// A4 column is illegible. The viewport crop reads like a real screenshot.
const VIEWPORT = { width: 1440, height: 900 }

async function login(page, id, password) {
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  // Re-auth overlay appears instead of the login form when a session lingers.
  const reauth = page.locator('.session-expired-modal input[type="password"]')
  if (await reauth.count()) {
    await reauth.fill(password)
    await page.getByRole('button', { name: /continue/i }).click()
  } else {
    await page.locator('input[type="text"]').first().fill(id)
    await page.locator('input[type="password"]').first().fill(password)
    await page.getByRole('button', { name: /sign in/i }).click()
  }
  await page.waitForTimeout(2500)
}

async function shot(page, name) {
  await page.waitForTimeout(1800) // let panel data + fade-ins settle
  await page.screenshot({ path: path.join(OUT, `${name}.png`) })
  console.log(`  captured ${name}.png`)
}

async function openTab(page, label) {
  await page.getByRole('tab', { name: label, exact: true }).click()
  await page.waitForTimeout(1200)
}

const browser = await chromium.launch()

// ── Clinician (doctor) surfaces ──
{
  const ctx = await browser.newContext({ viewport: VIEWPORT })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  await page.evaluate(() => localStorage.clear())
  await login(page, 'DOC001', 'password')
  await shot(page, 'vigil-ward-board')

  for (const [tab, name] of [
    ['Orders', 'orders-cpoe'],
    ['Messages', 'relay-case-rooms'],
    ['Sage', 'sage-assistant'],
    ['Scribe', 'scribe-documentation'],
    ['Lens', 'lens-image-analysis'],
    ['Forecast', 'forecast-census'],
    ['Command', 'command-center'],
    ['Simulator', 'what-if-simulator'],
    ['Revenue', 'revenue-integrity'],
    ['Appointments', 'appointments'],
  ]) {
    await openTab(page, tab)
    await shot(page, name)
  }
  await ctx.close()
}

// ── Pharmacist: Helix ──
{
  const ctx = await browser.newContext({ viewport: VIEWPORT })
  const page = await ctx.newPage()
  await login(page, 'PHA001', 'password')
  await shot(page, 'helix-pharmacy')
  await ctx.close()
}

// ── Admin: governance surfaces ──
{
  const ctx = await browser.newContext({ viewport: VIEWPORT })
  const page = await ctx.newPage()
  await login(page, 'ADM001', 'password')
  await shot(page, 'admin-panel')
  await ctx.close()
}

// ── Patient: Bridge portal ──
{
  const ctx = await browser.newContext({ viewport: VIEWPORT })
  const page = await ctx.newPage()
  await login(page, 'P001', 'patient123')
  await shot(page, 'bridge-patient-portal')
  await ctx.close()
}

// ── Login screen (signed out) ──
{
  const ctx = await browser.newContext({ viewport: VIEWPORT })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  await shot(page, 'clinician-login')
  await ctx.close()
}

await browser.close()
console.log(`\nAll screenshots written to ${OUT}`)
