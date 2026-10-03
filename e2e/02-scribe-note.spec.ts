import { test, expect } from '@playwright/test'
import { login, openTab } from './helpers'

/**
 * Golden journey #2 — Scribe documentation surface
 * ------------------------------------------------
 * A clinician opens Scribe and exercises the documentation flow as far as is
 * DETERMINISTIC without real audio or a live LLM:
 *   - the empty state ("Orb Scribe") renders,
 *   - the compose input + microphone/record controls render,
 *   - typing a note transitions the panel into the transcript/compose view and
 *     surfaces the note-format selector (SOAP / Discharge / I-PASS) and the
 *     "Structure Note" action — the entry point to the governance pipeline.
 *
 * We intentionally do NOT click "Structure Note": that calls the MedGemma/LLM
 * path (Ollama), which is not available in this environment, and its output is
 * non-deterministic. Asserting the UI + governance affordances render is the
 * robust contract.
 */
test('clinician opens Scribe and the compose + structure UI renders', async ({ page }) => {
  await login(page, 'doctor')
  await openTab(page, 'scribe')

  // Empty state.
  await expect(page.locator('.scribe-empty__title')).toHaveText('Orb Scribe')

  // The always-present composer: free-text input + record + live-stream buttons.
  const composer = page.locator('#scribe-input')
  await expect(composer).toBeVisible()
  await expect(composer).toHaveAttribute('placeholder', /Type clinical notes/i)
  await expect(page.locator('#scribe-record-btn')).toBeVisible()

  // Type a deterministic note (no audio needed). This drives `transcript` state.
  const note =
    'Patient resting comfortably. Vitals stable. Continue current management and review in the morning.'
  await composer.fill(note)
  await expect(composer).toHaveValue(note)

  // Typing flips the panel into the transcript/compose view, which reveals the
  // note-format governance selector and the Structure Note action.
  await expect(page.getByRole('button', { name: /Structure Note/i })).toBeVisible()

  // The format selector offers the three governed document types. THREE selects
  // share the `.scribe-format-select` class (transcription-language, patient
  // link, note format — the last two both inside `.scribe-actions`), so class
  // scoping is ambiguous; target the dedicated testid instead.
  const formatSelect = page.getByTestId('scribe-format-select')
  await expect(formatSelect).toBeVisible()
  await expect(formatSelect.locator('option')).toContainText([
    'SOAP Note',
    'Discharge Summary',
    'I-PASS Handoff',
  ])

  // The record button remains enabled (mic capture entry point) — proves the
  // dictation affordance is wired even though we don't exercise audio.
  await expect(page.locator('#scribe-record-btn')).toBeEnabled()
})
