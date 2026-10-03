import { test, expect } from '@playwright/test'
import { login, openTab } from './helpers'

/**
 * Golden journey #1 — Login + Vigil
 * ---------------------------------
 * A clinician logs in, lands on the shell, and sees the Vigil ("Patients")
 * panel render with at least one admitted patient row.
 *
 * This is the smoke test for "the app boots, auth works, and real seed data
 * reaches the UI". If this fails, nothing else is worth running.
 */
test('clinician logs in and sees the Vigil patient list', async ({ page }) => {
  // Surface any uncaught page errors to help debugging (non-fatal).
  const pageErrors: string[] = []
  page.on('pageerror', (e) => pageErrors.push(String(e)))

  await login(page, 'doctor')

  // The shell chrome is up: logout affordance present.
  await expect(page.locator('#btn-logout')).toBeVisible()

  // Vigil is a doctor's default landing tab, but click it explicitly so the
  // journey is deterministic regardless of default-tab ordering.
  await openTab(page, 'vigil')

  // The Vigil panel renders its metric header ("Admitted" / "Active Patients").
  await expect(page.getByText('Active Patients', { exact: false })).toBeVisible()

  // At least one patient row appears (seed loads 30). Patient rows use the
  // stable `.patient-row` class (VigilPanel.tsx / PatientRow).
  const rows = page.locator('.patient-row')
  await expect(rows.first()).toBeVisible({ timeout: 20_000 })
  expect(await rows.count(), 'expected at least one seeded patient in Vigil').toBeGreaterThan(0)

  // Sanity: the "Admitted" metric tile renders. Exact match on the full
  // scope-qualified label — the Sage brief also renders "N admitted
  // hospital-wide" text, so a bare substring match would trip strict mode.
  await expect(page.getByText('Admitted · My Dept', { exact: true })).toBeVisible()

  expect(pageErrors, `uncaught page errors: ${pageErrors.join('\n')}`).toEqual([])
})
