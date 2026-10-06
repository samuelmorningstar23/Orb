/**
 * Verify Queue rows say where the patient is, who wrote the order and when,
 * and name the source in words a pharmacist uses ("Sage, from notes",
 * "Sage action") rather than "Agentic".
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const ROWS = [
  {
    id: 11,
    patient_id: 'P001',
    patient: 'Asha Rao',
    drug_name: 'Enoxaparin',
    dose: 'REVIEW REQUIRED',
    route: 'REVIEW REQUIRED',
    frequency: 'REVIEW REQUIRED',
    source: 'llm_extracted',
    status: 'active',
    safety_alerts: [],
    max_severity: 'none',
    interaction_screened: true,
    coverage_note: null,
    screen_failed: false,
    ward: 'Ward 3',
    bed: '12',
    prescriber: 'Dr Mehta',
    started_at: '2026-10-03 04:45:00',
  },
  {
    id: 12,
    patient_id: 'P002',
    patient: 'Ravi Kumar',
    drug_name: 'Ceftriaxone',
    dose: '1g',
    route: 'IV',
    frequency: 'OD',
    source: 'agentic',
    status: 'active',
    safety_alerts: [],
    max_severity: 'none',
    interaction_screened: true,
    coverage_note: null,
    screen_failed: false,
  },
]

vi.mock('../../api/pharmacy', () => ({
  getPendingVerifications: async () => ROWS,
  verifyMedication: vi.fn(),
  rejectMedication: vi.fn(),
}))

import PharmacyVerifyQueue from './PharmacyVerifyQueue'

describe('PharmacyVerifyQueue row details', () => {
  it('shows ward, bed, prescriber and the charted time (as UTC, in clinical form)', async () => {
    render(<PharmacyVerifyQueue />)
    expect(await screen.findByText('Ward 3, bed 12')).toBeInTheDocument()
    // 04:45 UTC is 10:15 AM in India; the bare DB string must not be read as local time.
    const ordered = screen.getByText(/^Ordered by Dr Mehta, 3 Oct 2026, /)
    expect(ordered).toBeInTheDocument()
  })

  it('names the source in words, not as a code', async () => {
    render(<PharmacyVerifyQueue />)
    expect(await screen.findByText('Sage, from notes')).toBeInTheDocument()
    expect(screen.getByText('Sage action')).toBeInTheDocument()
    expect(screen.queryByText('Agentic')).not.toBeInTheDocument()
    expect(screen.queryByText('AI extracted')).not.toBeInTheDocument()
  })

  it('leaves the detail line out when the server sends none', async () => {
    render(<PharmacyVerifyQueue />)
    await screen.findByText('Ravi Kumar')
    expect(screen.queryByText(/Ordered by .*Ceftriaxone/)).not.toBeInTheDocument()
    expect(screen.getAllByText(/^Ordered by /)).toHaveLength(1)
  })
})

describe('PharmacyVerifyQueue names the people', () => {
  it('names the ordering person from ordered_by_name', async () => {
    (ROWS as unknown[]).push({ ...ROWS[1], id: 13, patient: 'Mina Das', ordered_by_name: 'Dr Neel Shah' })
    render(<PharmacyVerifyQueue />)
    expect(await screen.findByText('Ordered by Dr Neel Shah')).toBeInTheDocument()
    ROWS.pop()
  })
})

describe('PharmacyVerifyQueue provenance from a signed note', () => {
  it('says which note, shows the sentence, and opens the note', async () => {
    const pharmacy = await import('../../api/pharmacy')
    const getSourceNote = vi.fn(async () => ({
      medication_id: 14, patient_id: 'P003', note_id: 88, signed_by: 'Dr Rohan Pillai',
      signed_at: '2026-10-06T11:19:00+00:00',
      sentence: 'Start enoxaparin 40 mg SC once daily for VTE prophylaxis.',
      text: 'Day 2 after hip fracture repair.\nStart enoxaparin 40 mg SC once daily for VTE prophylaxis.',
    }))
    ;(pharmacy as unknown as { getSourceNote: unknown }).getSourceNote = getSourceNote
    ;(ROWS as unknown[]).push({
      ...ROWS[0], id: 14, patient: 'Gita Rao', drug_name: 'Enoxaparin',
      source_note_id: 88, source_sentence: 'Start enoxaparin 40 mg SC once daily for VTE prophylaxis.',
      source_note_signed_by: 'Dr Rohan Pillai', source_note_signed_at: '2026-10-06T11:19:00+00:00',
    })
    render(<PharmacyVerifyQueue />)
    // 11:19 UTC is 04:49 PM in India.
    expect(await screen.findByText('From the note signed by Dr Rohan Pillai at 04:49 PM on 6 Oct 2026')).toBeInTheDocument()
    expect(screen.getByText('“Start enoxaparin 40 mg SC once daily for VTE prophylaxis.”')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open the note' }))
    expect(await screen.findByRole('region', { name: 'Signed note for Enoxaparin' })).toHaveTextContent('Day 2 after hip fracture repair.')
    expect(getSourceNote).toHaveBeenCalledWith(14)
    ROWS.pop()
  })
})
