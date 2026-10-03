import { test, expect } from '@playwright/test'
import { login, openTab } from './helpers'

/**
 * Golden journey #4 — Pharmacist Verify Queue
 * -------------------------------------------
 * A pharmacist logs in and opens the "Verify Queue" tab. We assert the queue
 * surface renders. An EMPTY queue ("Queue clear" / "No AI/agentic medications
 * are awaiting verification.") is an acceptable, valid state — the seed does
 * not necessarily stage a pending AI-ordered medication, and the contract we
 * care about is that the pharmacist-only surface mounts and loads without error.
 */
test('pharmacist opens the Verify Queue and it renders', async ({ page }) => {
  await login(page, 'pharmacist')

  // Pharmacist role exposes: helix, pharmacy-verify, sage. Open Verify Queue.
  await openTab(page, 'pharmacy-verify')

  // The queue header always renders (PharmacyVerifyQueue.tsx).
  await expect(
    page.getByRole('heading', { name: /Pharmacist Verification Queue/i }),
  ).toBeVisible()

  // Wait out the loading state, then assert we land on EITHER the empty state
  // OR a populated queue with at least one row — both are valid.
  await expect(page.getByText(/Loading verification queue/i)).toHaveCount(0, {
    timeout: 15_000,
  })

  const emptyState = page.getByText(/No AI\/agentic medications are awaiting verification/i)
  const verifyButton = page.getByRole('button', { name: /^(Verify|…)$/ })

  const isEmpty = (await emptyState.count()) > 0
  const hasRows = (await verifyButton.count()) > 0

  expect(
    isEmpty || hasRows,
    'Verify Queue rendered neither an empty state nor any pending rows',
  ).toBeTruthy()

  if (isEmpty) {
    // "Queue clear" appears in more than one place (this panel's empty state is a
    // <div>Queue clear</div>; another background panel renders a longer "Queue
    // clear — no pending orders." string). Match the exact empty-state text.
    await expect(page.getByText('Queue clear', { exact: true })).toBeVisible()
  }
})
