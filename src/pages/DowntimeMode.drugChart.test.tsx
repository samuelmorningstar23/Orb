/**
 * The downtime page on an Orb Ward Record computer (Samuel's ruling, 9 October
 * 2026: hide the drug lists). No Active medications section, not even an empty
 * one, whether the profile or the snapshot says so; the allergy stays. And a
 * ward clerk, whom the server refuses every downtime route (vitals.read), gets
 * one plain sentence and no buttons, and nothing is asked.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches } from '../shellConfig'

const getLatestSnapshot = vi.fn()
const captureSnapshot = vi.fn()

vi.mock('../api/downtime', () => ({
  getLatestSnapshot: (...a: unknown[]) => getLatestSnapshot(...a),
  captureSnapshot: (...a: unknown[]) => captureSnapshot(...a),
  fetchWardPackHtml: vi.fn(),
}))

import DowntimeMode from './DowntimeMode'

function envelope(extra: Record<string, unknown> = {}) {
  return {
    id: 1, patient_id: 'P-1', summary: 'snapshot', created_by: 'auto',
    created_at: '2026-10-06T08:00:00Z',
    snapshot: {
      schema_version: 2, captured_at: '2026-10-06T08:00:00Z', patient_id: 'P-1',
      demographics: { name: 'Asha Rao', mrn: 'MRN-1' },
      allergies: [{ substance: 'Penicillin', reaction: 'Anaphylaxis', severity: 'severe', status: 'active' }],
      problems: [],
      medications: [{ drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral', frequency: 'TDS',
                      status: 'active' }],
      latest_vitals: null, news2: null, open_escalations: [], code_status: 'full_code',
      recent_notes: [], recent_risk_scores: [],
      counts: { allergies: 1, problems: 0, medications: 1, notes: 0 },
      ...extra,
    },
  }
}

function signIn(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: `U-${role}`, name: role, role }))
}

function answerHealth(clinical: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ modules: {}, clinical }),
  })))
}

const WARD_RECORD = { scoring_enabled: false, models_enabled: false, ward_record: true }
const FULL = { scoring_enabled: true, models_enabled: true, ward_record: false }

function renderPage() {
  return render(<MemoryRouter><DowntimeMode patientId="P-1" /></MemoryRouter>)
}

beforeEach(() => {
  getLatestSnapshot.mockReset()
  captureSnapshot.mockReset()
  signIn('nurse')
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  vi.unstubAllGlobals()
  localStorage.removeItem('orb_user')
})

describe('the downtime page and the drug chart', () => {
  it('draws no medicines on a Ward Record computer, even from a copy that holds one', async () => {
    answerHealth(WARD_RECORD)
    getLatestSnapshot.mockResolvedValue(envelope())
    const { container } = renderPage()
    await screen.findByText('Penicillin', { exact: false })
    expect(container.textContent).not.toMatch(/medicine|medication|Amoxicillin/i)
  })

  it('draws no medicines when the snapshot says drug_chart false, whatever the switches', async () => {
    answerHealth(FULL)
    getLatestSnapshot.mockResolvedValue(envelope({ medications: [], drug_chart: false }))
    const { container } = renderPage()
    await screen.findByText('Penicillin', { exact: false })
    expect(container.textContent).not.toMatch(/medicine|medication/i)
  })

  it('draws the medicines as before with the profile off', async () => {
    answerHealth(FULL)
    getLatestSnapshot.mockResolvedValue(envelope())
    renderPage()
    expect(await screen.findByRole('heading', { name: /Active medications \(1\)/ })).toBeTruthy()
    expect(screen.getByText('Amoxicillin')).toBeTruthy()
  })
})

describe('a role the downtime routes refuse', () => {
  it('a ward clerk gets one plain sentence, no buttons, and nothing is asked', async () => {
    signIn('ward_clerk')
    answerHealth(WARD_RECORD)
    renderPage()
    expect(await screen.findByText('Downtime copies are kept for nurses and doctors.')).toBeTruthy()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(document.body.textContent).not.toMatch(/Insufficient permissions/)
    expect(getLatestSnapshot).not.toHaveBeenCalled()
    expect(captureSnapshot).not.toHaveBeenCalled()
  })

  it('a doctor still gets the page and its buttons', async () => {
    signIn('doctor')
    answerHealth(WARD_RECORD)
    getLatestSnapshot.mockResolvedValue(envelope())
    renderPage()
    await screen.findByText('Penicillin', { exact: false })
    expect(screen.getByRole('button', { name: /Print ward pack/ })).toBeTruthy()
    expect(screen.queryByText('Downtime copies are kept for nurses and doctors.')).toBeNull()
  })
})
