/**
 * The downtime page and the handover page open in tabs of their own and draw
 * from the computer's switches, so they wait for them too (Samuel's ruling,
 * 9 October 2026: fix the startup flash). Until the switches are known they
 * draw the neutral frame and ask the server for nothing but /api/health; with
 * the probe down and nothing saved they say the server could not be reached.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, resetFeatureFlags, resetSwitchesKnown } from '../shellConfig'

const getLatestSnapshot = vi.fn(() => new Promise(() => {}))
vi.mock('../api/downtime', () => ({
  getLatestSnapshot: () => getLatestSnapshot(),
  captureSnapshot: vi.fn(),
  fetchWardPackHtml: vi.fn(),
}))
const getIpassDraft = vi.fn(() => new Promise(() => {}))
vi.mock('../api/ipass', () => ({
  getIpassDraft: () => getIpassDraft(),
  saveIpass: vi.fn(),
  prepareReasonedAssessment: vi.fn(() => new Promise(() => {})),
}))

import DowntimeMode from '../pages/DowntimeMode'
import HandoffPage from '../pages/HandoffPage'

const pages = {
  downtime: () => render(<MemoryRouter><DowntimeMode patientId="P-1" /></MemoryRouter>),
  handover: () => render(
    <MemoryRouter initialEntries={['/handoff/P-1']}>
      <Routes><Route path="/handoff/:patientId" element={<HandoffPage />} /></Routes>
    </MemoryRouter>,
  ),
}

async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve() })
}

beforeEach(() => {
  localStorage.clear()
  resetSwitchesKnown()
  resetFeatureFlags()
  getLatestSnapshot.mockClear()
  getIpassDraft.mockClear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  resetSwitchesKnown()
  resetFeatureFlags()
  resetClinicalSwitches()
  localStorage.clear()
})

describe.each(Object.entries(pages))('the %s page', (_name, renderPage) => {
  it('draws the neutral frame and reads no patient until the switches are known', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    renderPage()
    await settle()
    expect(screen.getByTestId('orb-startup-frame')).toBeTruthy()
    expect(screen.getByRole('status')).toHaveTextContent('Starting Orb on this computer')
    expect(getLatestSnapshot).not.toHaveBeenCalled()
    expect(getIpassDraft).not.toHaveBeenCalled()
  })

  it('says the server could not be reached when the probe fails with nothing saved', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    renderPage()
    await settle()
    expect(screen.getByRole('alert')).toHaveTextContent("Could not reach this computer's server")
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('opens once the probe answers', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({ modules: {}, clinical: { ward_record: false } }),
    })))
    renderPage()
    await settle()
    expect(screen.queryByTestId('orb-startup-frame')).toBeNull()
    expect(getLatestSnapshot.mock.calls.length + getIpassDraft.mock.calls.length).toBeGreaterThan(0)
  })
})
