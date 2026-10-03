import { expect, type Page } from '@playwright/test'

/**
 * Shared e2e helpers for the Orb golden-journey suite.
 *
 * Demo credentials come from server/scripts/seed_clinical_data.py (the seed the
 * running app uses). All demo staff passwords are "password" except where noted.
 * These are dev-only quick-access accounts, gated behind import.meta.env.DEV.
 */
export const CREDS = {
  doctor: { id: 'DOC001', password: 'password', name: 'Dr. Meera Sharma' },
  nurse: { id: 'NUR001', password: 'password', name: 'Priya Reddy' },
  pharmacist: { id: 'PHA001', password: 'password', name: 'Deepa Joshi' },
  admin: { id: 'ADM001', password: 'password', name: 'Rajesh Kumar' },
} as const

/**
 * Log in through the real staff login form and land on the clinician shell.
 *
 * Fails fast with a clear assertion message if the credentials are rejected,
 * rather than hanging until the global timeout. This keeps every spec
 * "skippable-graceful": a bad seed / wrong password surfaces as an explicit
 * "login failed" error, not a mystery timeout.
 */
export async function login(
  page: Page,
  who: keyof typeof CREDS,
): Promise<void> {
  const { id, password } = CREDS[who]

  await page.goto('/login/staff')

  // The staff login form (Login.tsx) exposes stable ids.
  await expect(
    page.locator('#input-employee-id'),
    'staff login form did not render (#input-employee-id missing)',
  ).toBeVisible()

  await page.locator('#input-employee-id').fill(id)
  await page.locator('#input-password').fill(password)

  // Watch the auth call so we can give a precise failure message.
  const loginResp = page.waitForResponse(
    (r) => r.url().includes('/api/auth/login') && r.request().method() === 'POST',
    { timeout: 20_000 },
  )
  await page.locator('#btn-login').click()

  const resp = await loginResp
  if (resp.status() !== 200) {
    const body = await resp.text().catch(() => '<unreadable>')
    throw new Error(
      `Login failed for ${who} (${id}): HTTP ${resp.status()} — ${body}. ` +
        `Is the backend seeded (server/scripts/seed_clinical_data.py) and running on :8000?`,
    )
  }

  // The app redirects to /dashboard and mounts the clinician shell.
  await page.waitForURL('**/dashboard**', { timeout: 20_000 })
  // A visible login error banner would mean the redirect never really happened.
  await expect(
    page.locator('.login__error'),
    'login error banner is visible after a 200 — unexpected',
  ).toHaveCount(0)
}

/** Log out via the shell's logout button and return to the login screen. */
export async function logout(page: Page): Promise<void> {
  const btn = page.locator('#btn-logout')
  if (await btn.count()) {
    await btn.click()
    await page.waitForURL('**/login/**', { timeout: 10_000 }).catch(() => {})
  }
}

/**
 * Click a shell nav item by tab id (ids are `nav-<tab>` in ClinicianShell.tsx)
 * and assert it becomes the active tab.
 */
export async function openTab(page: Page, tabId: string): Promise<void> {
  const nav = page.locator(`#nav-${tabId}`)
  await expect(
    nav,
    `nav item #nav-${tabId} is not available for this role`,
  ).toBeVisible()
  await nav.click()
  await expect(nav).toHaveClass(/shell__nav-item--active/)
}
