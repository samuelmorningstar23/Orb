/**
 * The Validate screen and critical results (lis-15): validating a critical
 * value says whether its case-room post went out, and the critical notice no
 * longer says Orb sends nothing. The reported-results section (lis-13) sits
 * below the queues and reads nothing until asked.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { QueueGroup, QueueResult, ValidationQueues } from '../../../api/lab/validation'
import ValidateView from './ValidateView'

vi.mock('../../../api/lab/validation', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/validation')>(
    '../../../api/lab/validation')
  return { ...actual, getValidationQueues: vi.fn(), techValidate: vi.fn() }
})
vi.mock('../../../api/lab/amendments', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/amendments')>(
    '../../../api/lab/amendments')
  return { ...actual, getReportedResults: vi.fn() }
})

const api = vi.mocked(await import('../../../api/lab/validation'))
const amendments = vi.mocked(await import('../../../api/lab/amendments'))

const critical: QueueResult = {
  result_id: 41, item_id: 1, order_id: 'O-1', status: 'entered',
  test: { id: 'LOINC:2823-3', name: 'Potassium', short_name: 'K', result_type: 'numeric' },
  value_text: '7.0', comparator: null, unit: 'mmol/L', abnormal_flag: 'HH',
  reference_range_text: '3.5 to 5.1 mmol/L', comment: null, is_critical: 1,
  critical_limit_text: 'Critical above 6.5 mmol/L', flag_text: 'Critically high',
  entered_by_name: 'Tech A', entered_at: '2026-10-09T04:00:00Z', tech_validated_by_name: null,
  tech_validated_at: null, returned: null, delta: null, needs_delta_reason: false, delta_ack: null,
}

const group: QueueGroup = {
  specimen_id: '261009000127', priority: 'stat',
  patient: { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', ward: 'GM-1', bed: '4A' },
  collected_at: '2026-10-09T03:30:00Z', received_at: '2026-10-09T03:50:00Z', results: [critical],
}

function queues(over: Partial<ValidationQueues> = {}): ValidationQueues {
  return { technical: [group], authorisation: [], can_authorise: false,
    require_distinct_authoriser: true, scope: { department: null, hospital_wide: true }, ...over }
}

const props = { meta: {}, state: 'on' as const, openView: vi.fn(), onModuleOff: vi.fn() }

afterEach(() => {
  vi.clearAllMocks()
})

describe('ValidateView and critical results', () => {
  it('says a critical is posted to the case room and the call is logged on Critical results', async () => {
    api.getValidationQueues.mockResolvedValue(queues())
    render(<ValidateView {...props} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('posts it to the patient\'s case room as preliminary')
    expect(alert.textContent).toContain('log the call with the read-back on Critical results')
    expect(alert.textContent).not.toContain('does not send')
  })

  it('reports how the ward was told after validation', async () => {
    api.getValidationQueues.mockResolvedValue(queues())
    api.techValidate.mockResolvedValue({ validated: 1, criticals: [
      { notification_id: 'LC-1', posted: false, reason: 'no_case_room', message_id: null }] } as never)
    render(<ValidateView {...props} />)
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Validate 1 result' }))
    expect(await screen.findByText(/1 result technically validated .*A critical result could not be posted\. Phone the ward now\./))
      .toBeTruthy()
  })

  it('offers the reported-results section and reads nothing until asked', async () => {
    api.getValidationQueues.mockResolvedValue(queues({ technical: [] }))
    render(<ValidateView {...props} />)
    expect(await screen.findByText('Amend or withdraw a reported result')).toBeTruthy()
    await waitFor(() => expect(api.getValidationQueues).toHaveBeenCalled())
    expect(amendments.getReportedResults).not.toHaveBeenCalled()
  })
})
