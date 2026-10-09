/**
 * The patient story on an Orb Ward Record computer (Samuel's ruling, 9 October
 * 2026: hide the drug lists). The server sends no medication or administration
 * event there; the story drops any it is sent too, so no Meds or eMAR chip and
 * no dose line is drawn. The allergy event and every other line stay.
 */
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'

const getPatientStoryFeed = vi.fn()

vi.mock('../../api/client', () => ({
  getPatientStoryFeed: (...a: unknown[]) => getPatientStoryFeed(...a),
  markChartReviewed: vi.fn(),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

import PatientStory from './PatientStory'

const EVENTS = [
  { timestamp: '2026-10-08T09:00:00Z', type: 'medication' as const, title: 'Started Amoxicillin', detail: '500 mg Oral TDS', actor: 'Dr Rao' },
  { timestamp: '2026-10-08T08:00:00Z', type: 'administration' as const, title: 'Gave Amoxicillin', detail: '500 mg Oral', actor: 'Nurse Iyer' },
  { timestamp: '2026-10-08T07:00:00Z', type: 'allergy' as const, title: 'Allergy recorded: Penicillin', detail: 'Anaphylaxis', actor: 'Dr Rao' },
  { timestamp: '2026-10-07T20:00:00Z', type: 'admission' as const, title: 'Admitted', detail: 'GM-1 · Bed 4', actor: null },
]

async function open() {
  getPatientStoryFeed.mockResolvedValue({ events: EVENTS, last_reviewed_at: null, new_count: 0 })
  render(<PatientStory patientId="P001" patientName="Geeta Sharma" onClose={() => {}} />)
  await waitFor(() => expect(screen.getByText('Admitted')).toBeInTheDocument())

}

beforeEach(() => { getPatientStoryFeed.mockReset() })
afterEach(() => { cleanup(); resetClinicalSwitches() })

describe('the patient story and the drug chart', () => {
  it('draws no medicine line or chip on a Ward Record computer, and keeps the allergy', async () => {
    act(() => { setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false }) })
    await open()
    const text = document.body.textContent || ''
    expect(text).not.toMatch(/Amoxicillin|500 mg|Meds|eMAR/)
    expect(text).toContain('Allergy recorded: Penicillin')
  })

  it('draws the medicine lines and chips with the profile off', async () => {
    await open()
    const text = document.body.textContent || ''
    expect(text).toContain('Started Amoxicillin')
    expect(text).toContain('Gave Amoxicillin')
    expect(screen.getByRole('button', { name: /Meds/ })).toBeTruthy()
  })
})
