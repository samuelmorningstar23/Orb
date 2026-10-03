import { test, expect } from '@playwright/test'
import { login, openTab } from './helpers'

/**
 * Golden journey #3 — Orders / CPOE-lite
 * --------------------------------------
 * A clinician opens the Orders tab, enters a real patient, creates an order
 * with a VALID terminology code (LOINC 58410-2 — "CBC panel", part of the
 * curated seed loaded by terminology_service.seed_terminology() on backend
 * startup), and asserts the new order appears in the list with a status chip.
 *
 * The create form validates codes against the local terminology subset, so we
 * drive the real Validate step and only submit once it reports ✓.
 */

// A real LOINC code from the curated seed (services/terminology_service.py).
const LOINC_CODE = '58410-2'

test('clinician creates a coded order and it appears with a status chip', async ({ page }) => {
  await login(page, 'doctor')
  await openTab(page, 'orders')

  // All panels are mounted at once (display toggled), so scope EVERY query to the
  // active Orders panel — other hidden panels (e.g. Revenue Integrity) reuse the
  // same placeholders/labels and would otherwise trip Playwright strict mode.
  const panel = page.locator('#panel-orders')

  // Panel header renders.
  await expect(panel.getByRole('heading', { name: 'Orders', exact: true })).toBeVisible()

  // Fetch a real patient id via the app's own API (through the Vite proxy).
  // Auth is now the HttpOnly `orb_session` cookie set at login (the JWT is
  // deliberately no longer in localStorage — XSS-exfiltration hardening).
  // page.request shares the browser context's cookie jar, so the cookie
  // authenticates this GET with no Authorization header. Random seed ids (PAT-*)
  // mean we must not hard-code one.
  const patientsResp = await page.request.get('/api/vigil/patients')
  expect(patientsResp.ok(), 'GET /api/vigil/patients failed').toBeTruthy()
  const patients = await patientsResp.json()
  const patientId: string = patients[0].id
  expect(patientId, 'no seeded patient available').toBeTruthy()

  // The panel (mounted without a patient prop) shows a Patient ID input.
  const patientInput = panel.getByPlaceholder('e.g. PAT001')
  await expect(patientInput).toBeVisible()
  await patientInput.fill(patientId)

  // Open the create-order form.
  await panel.getByRole('button', { name: /New Order/i }).click()

  // Order type defaults to "lab" (needs a code). Enter + validate the LOINC code.
  const codeInput = panel.getByPlaceholder('e.g. 2524-7')
  await expect(codeInput).toBeVisible()
  await codeInput.fill(LOINC_CODE)
  await panel.getByRole('button', { name: 'Validate', exact: true }).click()

  // Validation succeeds → a ✓ + the code display appears. Validation also
  // auto-fills the description with the display name when it's empty.
  await expect(panel.getByText('✓', { exact: false }).first()).toBeVisible({ timeout: 15_000 })

  // Ensure a description is present (belt-and-suspenders in case auto-fill
  // didn't run) then submit.
  const description = panel.getByPlaceholder('Order description')
  await expect(description).toBeVisible()
  if (!(await description.inputValue())) {
    await description.fill('CBC panel - Blood by Automated count')
  }
  const uniqueDesc = await description.inputValue()

  await panel.getByRole('button', { name: /Create Order/i }).click()

  // The new order shows up in the list with its description and a status chip.
  await expect(panel.getByText(uniqueDesc, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  })
  // New orders default to the "draft" status (orders_service state machine).
  await expect(panel.getByText('draft', { exact: true }).first()).toBeVisible()
  // The coded system+code is rendered on the card.
  await expect(panel.getByText(LOINC_CODE, { exact: false }).first()).toBeVisible()
})
