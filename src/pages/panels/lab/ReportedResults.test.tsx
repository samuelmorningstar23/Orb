/**
 * Amend or withdraw a reported result (lis-13), on the Validate screen.
 * What it must never get wrong:
 *
 *   · Nothing is read until a sample number is asked for (the read is
 *     audited).
 *   · An amendment is sent with the value as typed, the reason and what the
 *     row showed; while two signatories are required it says the amendment
 *     waits for a second one.
 *   · A waiting amendment can be confirmed only where the server allows it,
 *     and says who proposed it.
 *   · A withdrawal needs a reason and says when the result belonged to
 *     another patient.
 *   · How the ward was told is said in words.
 *   · Only a signatory sees the amend and withdraw controls.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ReportedResults as ReportedData, ReportedTest, ReportedVersion } from '../../../api/lab/amendments'
import ReportedResults from './ReportedResults'

vi.mock('../../../api/lab/amendments', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/amendments')>(
    '../../../api/lab/amendments')
  return {
    ...actual,
    getReportedResults: vi.fn(),
    amendResult: vi.fn(),
    confirmAmendment: vi.fn(),
    cancelAmendment: vi.fn(),
    withdrawResult: vi.fn(),
  }
})

const api = vi.mocked(await import('../../../api/lab/amendments'))

function version(over: Partial<ReportedVersion> = {}): ReportedVersion {
  return {
    result_id: 41, version: 1, status: 'released', value_text: '4.2', comparator: null,
    unit: 'mmol/L', abnormal_flag: '', flag_text: 'Within range',
    reference_range_text: '3.5 to 5.1 mmol/L', comment: null, is_critical: 0,
    critical_limit_text: null, entered_by_name: 'Tech A', entered_at: '2026-10-09T04:00:00Z',
    released_by_name: 'Dr Path', released_at: '2026-10-09T04:30:00Z', amend_reason: null,
    ...over,
  }
}

function test_(over: Partial<ReportedTest> = {}): ReportedTest {
  return {
    item_id: 1, order_id: 'O-1',
    test: { id: 'LOINC:2823-3', name: 'Potassium', short_name: 'K', result_type: 'numeric' },
    current: version(), history: [], withdrawn: null, pending_amendment: null, ...over,
  }
}

function data(over: Partial<ReportedData> = {}): ReportedData {
  return {
    specimen: { id: '261009000127', status: 'completed', collected_at: '2026-10-09T03:30:00Z',
      received_at: '2026-10-09T03:50:00Z',
      patient: { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', ward: 'GM-1', bed: '4A',
        discharged: false } },
    tests: [test_()],
    can_amend: true,
    require_distinct_authoriser: true,
    ...over,
  }
}

async function find() {
  fireEvent.change(screen.getByLabelText('Accession number of a reported sample'),
    { target: { value: '261009000127' } })
  fireEvent.click(screen.getByRole('button', { name: 'Find sample' }))
  await screen.findByText('Potassium')
}

beforeEach(() => {
  api.getReportedResults.mockResolvedValue(data())
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('ReportedResults', () => {
  it('reads nothing until a sample is asked for', () => {
    render(<ReportedResults onModuleOff={vi.fn()} />)
    expect(api.getReportedResults).not.toHaveBeenCalled()
  })

  it('proposes an amendment with the value as typed and what the row showed', async () => {
    api.amendResult.mockResolvedValue({ amended: false, proposed: true, proposal: null })
    render(<ReportedResults onModuleOff={vi.fn()} />)
    await find()
    expect(api.getReportedResults).toHaveBeenCalledWith('261009000127')
    fireEvent.click(screen.getByRole('button', { name: 'Amend' }))
    fireEvent.change(screen.getByLabelText('Amended value, Potassium'), { target: { value: '6.0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Propose amendment' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Say why')
    expect(api.amendResult).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Why it is amended, Potassium'),
      { target: { value: 'Transcription error' } })
    fireEvent.click(screen.getByRole('button', { name: 'Propose amendment' }))
    await waitFor(() => expect(api.amendResult).toHaveBeenCalledWith(41,
      { value: '6.0', reason: 'Transcription error', comment: null, delta_reason: null },
      { value_text: '4.2', comment: null, abnormal_flag: '' }))
    expect(await screen.findByText(/A second signatory must confirm it/)).toBeTruthy()
  })

  it('amends in one step where one signatory may, and says how the ward was told', async () => {
    api.getReportedResults.mockResolvedValue(data({ require_distinct_authoriser: false }))
    api.amendResult.mockResolvedValue({ amended: true, proposed: false,
      ward_notice: { posted: false, reason: 'no_case_room' }, criticals: [] })
    render(<ReportedResults onModuleOff={vi.fn()} />)
    await find()
    fireEvent.click(screen.getByRole('button', { name: 'Amend' }))
    fireEvent.change(screen.getByLabelText('Amended value, Potassium'), { target: { value: '5.0' } })
    fireEvent.change(screen.getByLabelText('Why it is amended, Potassium'),
      { target: { value: 'Wrong line' } })
    fireEvent.click(screen.getByRole('button', { name: 'Amend result' }))
    expect(await screen.findByText(/Potassium amended\. The ward was not told in Relay: this patient has no case room/)).toBeTruthy()
  })

  it('confirms a waiting amendment only where the server allows it', async () => {
    const pending = { proposal_id: 77, proposed_by_name: 'Dr Path', proposed_at: '2026-10-09T05:00:00Z',
      value_text: '6.0', unit: 'mmol/L', flag_text: 'High', comment: null,
      reason: 'Transcription error', delta_reason: null, you_proposed: false, may_confirm: true }
    api.getReportedResults.mockResolvedValue(data({ tests: [test_({ pending_amendment: pending })] }))
    api.confirmAmendment.mockResolvedValue({ amended: true, proposed: false,
      ward_notice: { posted: true, reason: null }, criticals: [] })
    render(<ReportedResults onModuleOff={vi.fn()} />)
    await find()
    expect(screen.getByText(/Amendment waiting for a second signatory: 6.0 mmol\/L/)).toBeTruthy()
    expect(screen.getByText(/Proposed by Dr Path/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Amend' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm amendment' }))
    await waitFor(() => expect(api.confirmAmendment).toHaveBeenCalledWith(41, pending,
      { value_text: '4.2', comment: null, abnormal_flag: '' }))
  })

  it('offers no confirm to the signatory who proposed it', async () => {
    const pending = { proposal_id: 77, proposed_by_name: 'Me', proposed_at: null, value_text: '6.0',
      unit: 'mmol/L', flag_text: 'High', comment: null, reason: 'x', delta_reason: null,
      you_proposed: true, may_confirm: false }
    api.getReportedResults.mockResolvedValue(data({ tests: [test_({ pending_amendment: pending })] }))
    render(<ReportedResults onModuleOff={vi.fn()} />)
    await find()
    expect(screen.queryByRole('button', { name: 'Confirm amendment' })).toBeNull()
    expect(screen.getByText(/Another signatory must confirm it/)).toBeTruthy()
  })

  it('withdraws with a reason and says it belonged to another patient', async () => {
    api.withdrawResult.mockResolvedValue({
      order: { id: 'O-1', status: 'resulted', conflict: true }, open_criticals: 1,
      incident_prompt: { incident_type: 'patient_identification', message: 'Report a patient identification incident in the NABH register.' },
      ward_notice: { posted: true, reason: null } })
    render(<ReportedResults onModuleOff={vi.fn()} />)
    await find()
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw' }))
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw result' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Say why')
    fireEvent.change(screen.getByLabelText('Why it is withdrawn, Potassium'),
      { target: { value: 'Another patient' } })
    fireEvent.click(screen.getByLabelText(/belonged to another patient/))
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw result' }))
    await waitFor(() => expect(api.withdrawResult).toHaveBeenCalledWith(41, 'Another patient', true,
      { value_text: '4.2', comment: null, abnormal_flag: '' }))
    const note = await screen.findByText(/Potassium withdrawn/)
    expect(note.textContent).toContain("The ward was told in the patient's case room.")
    expect(note.textContent).toContain('still shows resulted')
    expect(note.textContent).toContain('A critical result raised for it is still open')
    expect(note.textContent).toContain('patient identification incident')
  })

  it('shows the earlier versions and a withdrawal, and no controls to a technologist', async () => {
    api.getReportedResults.mockResolvedValue(data({
      can_amend: false,
      tests: [test_({ current: version({ result_id: 42, version: 2, value_text: '5.0',
        amend_reason: 'Wrong line' }), history: [version({ status: 'superseded' })] })],
    }))
    render(<ReportedResults onModuleOff={vi.fn()} />)
    await find()
    expect(screen.getByText(/Version 1: 4.2 mmol\/L \(Within range\), replaced/)).toBeTruthy()
    expect(screen.getByText(/On the chart: version 2/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Amend' })).toBeNull()
    expect(screen.getByText('A signatory amends or withdraws a reported result.')).toBeTruthy()
  })
})
