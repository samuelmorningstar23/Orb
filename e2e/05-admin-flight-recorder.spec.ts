import { test, expect } from '@playwright/test'
import { login, openTab } from './helpers'

/**
 * Golden journey #5 — Admin → Flight Recorder
 * -------------------------------------------
 * An admin opens Admin → Flight Recorder and asserts the forensic timeline
 * plus the hash-chain integrity badge render.
 *
 * The Flight Recorder replays Orb's hash-chained audit + event log and shows a
 * green "Chain verified · N" badge (or a red "Tampered at #N" badge on a chain
 * break). Because the running app already emits audit/event rows (logins,
 * vitals, deterioration events), the timeline should be populated.
 */
test('admin opens Flight Recorder and sees timeline + chain-integrity badge', async ({
  page,
}) => {
  await login(page, 'admin')

  // Admin role exposes an "admin" tab; open it, then the Flight Recorder sub-tab.
  await openTab(page, 'admin')

  const flightTab = page.locator('#admin-tab-flight')
  await expect(flightTab, 'admin sub-tab #admin-tab-flight missing').toBeVisible()
  await flightTab.click()

  // The Flight Recorder header renders.
  await expect(page.getByRole('heading', { name: 'Flight Recorder' })).toBeVisible()

  // Chain-integrity badge renders (verified or tampered). Scope to the header:
  // the per-event detail pane also renders a `.fr-badge` ("verified"), so an
  // unscoped `.fr-badge` matches two elements. The header badge is the summary.
  const badge = page.locator('.fr-header-right .fr-badge')
  await expect(badge).toBeVisible({ timeout: 15_000 })
  await expect(badge).toHaveText(/Chain verified|Tampered at/i)

  // The replay timeline renders with at least one recorded event. (If the scope
  // were genuinely empty the panel shows "No recorded events" — but a running
  // app always has audit rows, so we expect a populated timeline here.)
  const timeline = page.locator('.fr-timeline')
  await expect(timeline).toBeVisible({ timeout: 15_000 })
  await expect(page.locator('.fr-tl-item').first()).toBeVisible()

  // Transport controls (play/step/scrub) are present — proves the replay
  // surface, not just a static list, mounted.
  await expect(page.locator('.fr-transport')).toBeVisible()
})
