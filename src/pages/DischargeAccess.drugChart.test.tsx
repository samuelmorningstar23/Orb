/**
 * The discharge link page on an Orb Ward Record computer (Samuel's ruling, 9
 * October 2026: hide the drug lists). The server says drug_chart false and
 * sends no medicines; the page then prints no medicines section at all, and
 * the allergy still shows. A server that says nothing prints them as before.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const verifyDischargeAccess = vi.fn<(...a: unknown[]) => Promise<{ access_grant: string }>>(async () => ({ access_grant: 'grant' }))
const getVerifiedDischargeSummary = vi.fn<(...a: unknown[]) => Promise<unknown>>()

vi.mock('../api/client', () => ({
  verifyDischargeAccess: (...a: unknown[]) => verifyDischargeAccess(...a),
  getVerifiedDischargeSummary: (...a: unknown[]) => getVerifiedDischargeSummary(...a),
}))

import DischargeAccess from './DischargeAccess'

const BASE = {
  status: 'success', timeline: [],
  patient: { mrn: 'MRN-0021', name: 'Ravi Kumar', age: 58, sex: 'M', admission_id: 'ADM-0021' },
  allergies: [{ substance: 'Penicillin', reaction: 'Rash', severity: 'moderate' }],
  comorbidities: [], unverified_medication_count: 0, site_name: null,
  discharged_at: '2026-10-03 01:00:00', summary: 'Admitted with pneumonia. Recovered.',
  summary_status: 'reviewed', reviewed_by_name: 'Dr Meera Raman', reviewed_at: '2026-10-03T02:00:00Z',
}
const MEDS = [{ name: 'Amoxicillin', drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral',
                frequency: 'TDS', status: 'active' }]

async function openLink() {
  render(
    <MemoryRouter initialEntries={['/discharge/tok']}>
      <Routes><Route path="/discharge/:token" element={<DischargeAccess />} /></Routes>
    </MemoryRouter>,
  )
  fireEvent.change(screen.getByLabelText('Date of Birth'), { target: { value: '1964-03-11' } })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Access Records/ })) })
  await screen.findByText('Admitted with pneumonia. Recovered.')
}

beforeEach(() => {
  vi.clearAllMocks()
  window.print = vi.fn()
})

describe('the discharge link on a Ward Record computer', () => {
  it('prints no medicines section, even with a medicine sent, and keeps the allergy', async () => {
    getVerifiedDischargeSummary.mockResolvedValue({ ...BASE, medications: MEDS, drug_chart: false,
                                                     unverified_medication_count: 1 })
    await openLink()
    expect(document.body.textContent).not.toMatch(/medicine|medication|Amoxicillin/i)
    expect(document.body.textContent).toContain('Penicillin')
  })

  it('prints the medicines when the server says nothing about the drug chart', async () => {
    getVerifiedDischargeSummary.mockResolvedValue({ ...BASE, medications: MEDS })
    await openLink()
    expect(document.body.textContent).toContain('DISCHARGE MEDICINES')
    expect(document.body.textContent).toContain('Amoxicillin')
  })
})
