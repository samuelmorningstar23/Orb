/**
 * The downtime page with scoring switched off (the Orb Ward Record profile,
 * 6 October 2026). The server sends the snapshot with news2 null, no stored
 * risk rows and no escalation chain. The page must draw no NEWS2 badge, no
 * drivers line and no escalation field, and that holds even for a copy that
 * still carries a score (one saved on this device before the switch).
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, setClinicalSwitches } from '../shellConfig'

const getLatestSnapshot = vi.fn()

vi.mock('../api/downtime', () => ({
  getLatestSnapshot: (...a: unknown[]) => getLatestSnapshot(...a),
  captureSnapshot: vi.fn(),
  fetchWardPackHtml: vi.fn(),
}))

import DowntimeMode from './DowntimeMode'

function envelope(news2: Record<string, unknown> | null, escalations: unknown[] = []) {
  return {
    id: 1, patient_id: 'P-1', summary: 'snapshot', created_by: 'auto',
    created_at: '2026-10-06T08:00:00Z',
    snapshot: {
      schema_version: 2, captured_at: '2026-10-06T08:00:00Z', patient_id: 'P-1',
      demographics: { name: 'Asha Rao', mrn: 'MRN-1' },
      allergies: [], problems: [], medications: [],
      latest_vitals: {
        timestamp: '2026-10-06T07:50:00Z', heart_rate: 135, systolic_bp: 85, diastolic_bp: 50,
        spo2: 88, temperature: 39.5, respiratory_rate: 30, consciousness: 'Alert', on_oxygen: 0,
      },
      news2, open_escalations: escalations, code_status: 'full_code',
      recent_notes: [], recent_risk_scores: [],
      counts: { allergies: 0, problems: 0, medications: 0, notes: 0 },
    },
  }
}

function renderPage() {
  return render(<MemoryRouter><DowntimeMode patientId="P-1" /></MemoryRouter>)
}

beforeEach(() => {
  getLatestSnapshot.mockReset()
  // The page asks /api/health itself; this one answers scoring off.
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => ({ modules: {}, clinical: { scoring_enabled: false, ward_record: true } }),
  })))
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  vi.unstubAllGlobals()
})

describe('the downtime page with scoring switched off', () => {
  it('a snapshot with no score shows the readings and no NEWS2 at all', async () => {
    getLatestSnapshot.mockResolvedValue(envelope(null))
    const { container } = renderPage()
    await screen.findByRole('heading', { name: /latest vitals/i })
    expect(container.querySelector('.dt-badge')).toBeNull()
    expect(container.querySelector('.dt-drivers')).toBeNull()
    expect(container.textContent).not.toMatch(/NEWS2/)
    expect(container.textContent).toMatch(/88%/)
  })

  it('a copy that still carries a score and an escalation shows neither', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    getLatestSnapshot.mockResolvedValue(envelope(
      { raw_news2: 14, level: 'critical', spo2_scale: 1, drivers: ['Low SpO2 (88)'] },
      [{ status: 'raised', current_tier: 1, max_tier: 3, raised_at: '2026-10-06T07:55:00Z' }],
    ))
    const { container } = renderPage()
    await screen.findByRole('heading', { name: /latest vitals/i })
    expect(container.textContent).not.toMatch(/NEWS2|Low SpO2 \(88\)/)
    expect(screen.queryByText('Escalation')).toBeNull()
    expect(screen.getByRole('heading', { name: 'Code status' })).toBeTruthy()
  })
})
