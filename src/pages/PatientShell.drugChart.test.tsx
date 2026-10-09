/**
 * Bridge My Records on an Orb Ward Record computer (Samuel's ruling, 9 October
 * 2026: hide the drug lists). The records answer says drug_chart false and
 * carries no medicine; the portal then draws no Medications button and no
 * medicine word. With the profile off the button and the list are as before.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, setClinicalSwitches } from '../shellConfig'

const reports = vi.fn()
vi.mock('../App', () => ({
  useAuth: () => ({ user: { id: 'P001', name: 'Raj Patel', role: 'patient' }, logout: vi.fn() }),
}))
vi.mock('../api/client', () => ({
  getBridgeStatus: () => Promise.resolve({ patient_name: 'Raj Patel', status: 'stable', status_message: 'Monitoring.' }),
  getBridgeReports: () => reports(),
  askBridge: vi.fn(),
  apiFetch: () => Promise.resolve({ appointments: [] }),
}))
vi.mock('../components/bridge/CareTeamCard', () => ({ CareTeamCard: () => null }))
vi.mock('../components/bridge/VitalsTrendsCard', () => ({ VitalsTrendsCard: () => null }))

import PatientShell from './PatientShell'

const MED = { drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral', frequency: 'TDS',
              status: 'active', started_at: '2026-10-01 09:00:00' }
const REPORTS = {
  patient_id: 'PAT1', medications: [MED], clinical_documents: [], labs: [],
  imaging_reports: [], discharge_summary: null,
}

beforeEach(() => { reports.mockReset() })
afterEach(() => { cleanup(); resetClinicalSwitches() })

async function renderShell() {
  render(<PatientShell />)
  await waitFor(() => expect(reports).toHaveBeenCalled())
  await screen.findByRole('button', { name: /Report History/ })
}

describe('Bridge My Records and the drug chart', () => {
  it('draws no Medications button on a Ward Record computer, whatever is sent', async () => {
    act(() => { setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false }) })
    reports.mockResolvedValue(REPORTS)
    await renderShell()
    expect(screen.queryByRole('button', { name: /Medications/ })).toBeNull()
    expect(document.body.textContent).not.toMatch(/medicine|medication|Amoxicillin/i)
  })

  it('draws no Medications button when the records answer says drug_chart false', async () => {
    reports.mockResolvedValue({ ...REPORTS, medications: [], drug_chart: false })
    await renderShell()
    await act(async () => {})
    expect(screen.queryByRole('button', { name: /Medications/ })).toBeNull()
  })

  it('draws the Medications button and list with the profile off', async () => {
    reports.mockResolvedValue({ ...REPORTS, drug_chart: true })
    await renderShell()
    const button = await screen.findByRole('button', { name: /Medications/ })
    await act(async () => { fireEvent.click(button) })
    expect(await screen.findByText(/Amoxicillin/)).toBeTruthy()
  })
})
