/**
 * The handoff page as a page (F083, 2026-10-03): its tab is named after the
 * patient, its two actions use the shared button primitives and wrap, and a
 * failed save reads as a failure rather than in the info colour of a success.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getIpassDraft = vi.fn()
const saveIpass = vi.fn()
const prepareReasonedAssessment = vi.fn()

vi.mock('../../api/ipass', () => ({
  getIpassDraft: (...a: unknown[]) => getIpassDraft(...a),
  saveIpass: (...a: unknown[]) => saveIpass(...a),
  prepareReasonedAssessment: (...a: unknown[]) => prepareReasonedAssessment(...a),
}))

import IpassHandoffView from './IpassHandoffView'
import HandoffPage from '../../pages/HandoffPage'
import { markSwitchesKnownForTests } from '../../shellConfig'

const DRAFT = {
  patient_id: 'P-TEST-1',
  generated_at: '2026-10-03T09:00:00Z',
  window_hours: 12,
  source_summary: '1 vitals reading',
  plain_text: 'plain text handoff',
  sections: {
    illness_severity: {
      severity: 'Stable', news2: 1, news2_level: 'low',
      scoring_model: 'NEWS2', drivers: [], text: 'Stable.',
    },
    patient_summary: {
      name: 'Asha Rao', mrn: 'MRN-1', age: 44, sex: 'F', ward: 'W1', bed: '3',
      diagnosis: 'Pneumonia', problems: [], allergies: [], text: 'Asha Rao',
    },
    action_list: { active_medications: [], open_care_bundles: [], outstanding_items: [] },
    situation_awareness: {
      trend: 'stable', trend_display: 'stable', trend_detail: 'no change',
      active_deterioration: false,
      latest_vitals: {
        timestamp: '2026-10-03T08:40:00Z', heart_rate: 80, systolic_bp: 120,
        diastolic_bp: 70, spo2: 97, temperature: 36.8, respiratory_rate: 16,
        consciousness: 'Alert',
      },
      readings_count: 1, contingencies: [], text: '',
    },
    synthesis: { read_back_required: true, confirmed: false, text: 'Read back required.' },
    ai_assessment: null,
  },
}

beforeEach(() => {
  getIpassDraft.mockReset()
  saveIpass.mockReset()
  prepareReasonedAssessment.mockReset()
  prepareReasonedAssessment.mockResolvedValue({ status: 'disabled', assessment: null })
  document.title = 'Orb'
})

// These screens render before any health probe answers. The switches count
// as known, as on a page the server served; the startup rule itself is
// tested in ClinicianShell.startup.test.tsx.
beforeEach(() => { markSwitchesKnownForTests() })

describe('the browser tab title', () => {
  it('names the patient and bed on the handoff page, and restores on leaving', async () => {
    getIpassDraft.mockResolvedValue(DRAFT)
    const { unmount } = render(
      <MemoryRouter initialEntries={['/handoff/P-TEST-1']}>
        <Routes><Route path="/handoff/:patientId" element={<HandoffPage />} /></Routes>
      </MemoryRouter>,
    )
    await screen.findByText('I-PASS Shift Handoff')
    await waitFor(() => expect(document.title).toBe('Handoff: Asha Rao (3)'))
    unmount()
    expect(document.title).toBe('Orb')
  })

  it('leaves the tab alone when the view is embedded', async () => {
    getIpassDraft.mockResolvedValue(DRAFT)
    render(<IpassHandoffView patientId="P-TEST-1" />)
    await screen.findByText('I-PASS Shift Handoff')
    expect(document.title).toBe('Orb')
  })
})

describe('the actions', () => {
  it('use the shared primary and secondary buttons', async () => {
    getIpassDraft.mockResolvedValue(DRAFT)
    render(<IpassHandoffView patientId="P-TEST-1" />)
    expect(await screen.findByRole('button', { name: 'Save handoff' })).toHaveClass('btn-primary')
    expect(screen.getByRole('button', { name: 'Print' })).toHaveClass('btn-glass')
  })

  it('show a failed save as an alert in the danger text colour', async () => {
    getIpassDraft.mockResolvedValue(DRAFT)
    saveIpass.mockRejectedValue(new Error('Could not save the handoff note'))
    render(<IpassHandoffView patientId="P-TEST-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Save handoff' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not save the handoff note')
    expect(alert.getAttribute('style')).toContain('var(--status-danger-text)')
  })

  it('show a successful save as status in the info text colour', async () => {
    getIpassDraft.mockResolvedValue(DRAFT)
    saveIpass.mockResolvedValue({ id: 42 })
    render(<IpassHandoffView patientId="P-TEST-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Save handoff' }))
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Handoff saved as draft note #42.')
    expect(status.getAttribute('style')).toContain('var(--status-info-text)')
  })
})
