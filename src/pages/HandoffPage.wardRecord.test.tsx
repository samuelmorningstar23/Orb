/**
 * The handover page under the Orb Ward Record profile (6 October 2026). It
 * opens in a tab of its own, so it asks /api/health itself; with the profile
 * on, the handover is switched off and the page says so plainly instead of
 * drafting (every I-PASS route answers 404 then anyway).
 */
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, resetModuleFlags } from '../shellConfig'

const getIpassDraft = vi.fn(() => new Promise(() => {}))
vi.mock('../api/ipass', () => ({
  getIpassDraft: () => getIpassDraft(),
  saveIpass: vi.fn(),
  prepareReasonedAssessment: vi.fn(() => new Promise(() => {})),
}))

import HandoffPage from './HandoffPage'

function page(health: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => health })))
  return render(
    <MemoryRouter initialEntries={['/handoff/P-1']}>
      <Routes><Route path="/handoff/:patientId" element={<HandoffPage />} /></Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  resetModuleFlags()
  vi.unstubAllGlobals()
  getIpassDraft.mockClear()
})

describe('the handover page', () => {
  it('says the handover is not part of Orb Ward Record under the profile', async () => {
    page({ modules: {}, clinical: { ward_record: true, scoring_enabled: false, models_enabled: false } })
    expect(await screen.findByText('Not part of Orb Ward Record')).toBeTruthy()
    expect(screen.getByText(/The handover is not used on this computer/)).toBeTruthy()
  })

  it('drafts as before on an ordinary site', async () => {
    page({ modules: {}, clinical: { ward_record: false } })
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByText('Not part of Orb Ward Record')).toBeNull()
    expect(getIpassDraft).toHaveBeenCalled()
  })
})
