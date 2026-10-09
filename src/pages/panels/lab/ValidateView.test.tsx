/**
 * The Validate view (lis-11, lis-12) and the delta reason on the entry grid.
 * What they must never get wrong:
 *
 *   · Technical validation sends exactly the results ticked, each with what
 *     its row showed; a delta flag with no reason cannot be ticked, and its
 *     reason is sent with what the row showed.
 *   · Only a signatory sees the authorise controls; a result the signatory
 *     took part in cannot be ticked while two different people are required,
 *     and the row says why.
 *   · Authorising names one sample and only the results ticked on it.
 *   · A critical result says so in words with the instruction to phone the
 *     ward.
 *   · Sending a result back needs a reason.
 *   · A refusal because the screen is out of date reloads the lists.
 *   · A failed load is never an empty list.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../../api/lab'
import type { WorklistItem } from '../../../api/lab/results'
import type { QueueGroup, QueueResult, ValidationQueues } from '../../../api/lab/validation'
import ResultEntryGrid from './ResultEntryGrid'
import ValidateView, * as validateModule from './ValidateView'
import { discoverViews } from './viewRegistry'

vi.mock('../../../api/lab/validation', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/validation')>(
    '../../../api/lab/validation')
  return {
    ...actual,
    getValidationQueues: vi.fn(),
    acknowledgeDelta: vi.fn(),
    techValidate: vi.fn(),
    authoriseAndRelease: vi.fn(),
    returnToBench: vi.fn(),
  }
})

const api = vi.mocked(await import('../../../api/lab/validation'))

const ASHA = { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', ward: 'GM-1', bed: '4A' }

const FLAGGED_DELTA = {
  status: 'flagged' as const,
  note: 'Changed by +1.5 mmol/L from the earlier 4.0 mmol/L, at or beyond the delta limit of 1 mmol/L. Give a reason before technical validation.',
  kind: 'absolute' as const,
  threshold_text: '1 mmol/L',
  window_hours: 48,
  previous: { observation_id: 7, value_text: '4.0', unit: 'mmol/L',
    observed_at: '2026-10-08T04:00:00Z', source: 'ingested_hl7', from_laboratory: false },
  change_text: '+1.5 mmol/L',
  percent_text: null,
}

function result(over: Partial<QueueResult> = {}): QueueResult {
  return {
    result_id: 41,
    item_id: 1,
    order_id: 'O-1',
    status: 'entered',
    test: { id: 'LOINC:2823-3', name: 'Potassium', short_name: 'K', result_type: 'numeric' },
    value_text: '4.4',
    comparator: null,
    unit: 'mmol/L',
    abnormal_flag: '',
    reference_range_text: '3.5 to 5.1 mmol/L',
    comment: null,
    is_critical: 0,
    critical_limit_text: null,
    flag_text: 'Within range',
    entered_by_name: 'Tech A',
    entered_at: '2026-10-09T04:00:00Z',
    tech_validated_by_name: null,
    tech_validated_at: null,
    returned: null,
    delta: null,
    needs_delta_reason: false,
    delta_ack: null,
    ...over,
  }
}

function group(results: QueueResult[], over: Partial<QueueGroup> = {}): QueueGroup {
  return { specimen_id: '261009000127', priority: 'routine', patient: ASHA,
    collected_at: '2026-10-09T03:30:00Z', received_at: '2026-10-09T03:50:00Z', results, ...over }
}

function queues(over: Partial<ValidationQueues> = {}): ValidationQueues {
  return { technical: [], authorisation: [], can_authorise: false,
    require_distinct_authoriser: true, scope: { department: null, hospital_wide: true }, ...over }
}

const props = { meta: {}, state: 'on' as const, openView: vi.fn(), onModuleOff: vi.fn() }

beforeEach(() => {
  api.getValidationQueues.mockResolvedValue(queues())
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('ValidateView', () => {
  it('declares itself to the panel as the validate tab', () => {
    const [entry] = discoverViews({ './lab/ValidateView.tsx': validateModule })
    expect(entry).toMatchObject({ id: 'validate', label: 'Validate', order: 50,
      permission: 'lab.result', whenDraining: 'show' })
  })

  it('validates exactly the ticked results, each with what its row showed', async () => {
    const potassium = result()
    const sodium = result({ result_id: 42, item_id: 2, value_text: '139',
      test: { id: 'LOINC:2951-2', name: 'Sodium', short_name: 'Na', result_type: 'numeric' } })
    api.getValidationQueues.mockResolvedValue(queues({ technical: [group([potassium, sodium])] }))
    api.techValidate.mockResolvedValue({ validated: 1 })
    render(<ValidateView {...props} />)
    const table = await screen.findByRole('table', { name: /Technical validation/ })
    fireEvent.click(within(table).getByLabelText('Include Sodium'))
    fireEvent.click(screen.getByRole('button', { name: 'Validate 1 result' }))
    await waitFor(() => expect(api.techValidate).toHaveBeenCalledTimes(1))
    expect(api.techValidate).toHaveBeenCalledWith([
      { result_id: 41, expected: { value_text: '4.4', comment: null, abnormal_flag: '' } },
    ])
    expect(await screen.findByText(/1 result technically validated/)).toBeTruthy()
    // The lists load again after the step.
    expect(api.getValidationQueues).toHaveBeenCalledTimes(2)
  })

  it('will not validate a delta flag until a reason is given', async () => {
    const flagged = result({ value_text: '5.5', delta: FLAGGED_DELTA, delta_flag: 1,
      needs_delta_reason: true } as Partial<QueueResult>)
    api.getValidationQueues.mockResolvedValue(queues({ technical: [group([flagged])] }))
    api.acknowledgeDelta.mockResolvedValue({ result: {} as never, acknowledged: true })
    render(<ValidateView {...props} />)
    const box = await screen.findByLabelText('Include Potassium')
    expect((box as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Validate 0 results' }) as HTMLButtonElement).disabled)
      .toBe(true)
    expect(screen.getByText(/1 delta flag needs a reason/)).toBeTruthy()
    expect(screen.getByText(/at or beyond the delta limit/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Say why')
    expect(api.acknowledgeDelta).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Delta reason, Potassium'),
      { target: { value: 'On potassium replacement' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
    await waitFor(() => expect(api.acknowledgeDelta).toHaveBeenCalledWith(
      41, 'On potassium replacement', { value_text: '5.5', comment: null, abnormal_flag: '' }))
    expect(await screen.findByText(/delta reason saved/)).toBeTruthy()
  })

  it('shows a technician the results waiting for a signatory without the controls', async () => {
    const waiting = result({ status: 'tech_validated', tech_validated_by_name: 'Tech B',
      you_took_part: true, may_authorise: false })
    api.getValidationQueues.mockResolvedValue(queues({ authorisation: [group([waiting])],
      can_authorise: false }))
    render(<ValidateView {...props} />)
    const table = await screen.findByRole('table', { name: /Authorisation/ })
    expect(within(table).queryByRole('checkbox')).toBeNull()
    expect(screen.queryByRole('button', { name: /Authorise and release/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Send back' })).toBeNull()
    expect(screen.getByText('A signatory authorises these results.')).toBeTruthy()
  })

  it('lets a signatory release only the results she did not take part in', async () => {
    const mine = result({ status: 'tech_validated', you_took_part: true, may_authorise: false })
    const theirs = result({ result_id: 43, item_id: 3, status: 'tech_validated',
      value_text: '139', you_took_part: false, may_authorise: true,
      test: { id: 'LOINC:2951-2', name: 'Sodium', short_name: 'Na', result_type: 'numeric' } })
    api.getValidationQueues.mockResolvedValue(queues({ authorisation: [group([mine, theirs])],
      can_authorise: true }))
    api.authoriseAndRelease.mockResolvedValue({
      specimen: { id: '261009000127', status: 'in_process', moved_to: null }, released: 1,
      observation_ids: [9], orders: [{ id: 'O-3', status: 'in_progress', conflict: false }],
      one_person: 0 })
    render(<ValidateView {...props} />)
    const own = await screen.findByLabelText('Authorise Potassium')
    expect((own as HTMLInputElement).disabled).toBe(true)
    expect(screen.getByText(/You took part in this result. Another signatory must authorise it./))
      .toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Authorise and release 1 result' }))
    await waitFor(() => expect(api.authoriseAndRelease).toHaveBeenCalledWith('261009000127', [
      { result_id: 43, expected: { value_text: '139', comment: null, abnormal_flag: '' } },
    ]))
    expect(await screen.findByText(/1 result released to the chart for Asha Menon/)).toBeTruthy()
  })

  it('says a critical result in words and asks for a phone call', async () => {
    const critical = result({ status: 'tech_validated', value_text: '7.0', abnormal_flag: 'HH',
      flag_text: 'Critically high', is_critical: 1,
      critical_limit_text: 'Critical below 2.5 or above 6.5 mmol/L', may_authorise: true })
    api.getValidationQueues.mockResolvedValue(queues({ authorisation: [group([critical])],
      can_authorise: true }))
    render(<ValidateView {...props} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Critical result: Potassium 7.0 mmol/L')
    expect(alert.textContent).toContain('Critical below 2.5 or above 6.5 mmol/L')
    expect(alert.textContent).toContain('Phone the ward now')
    expect(screen.getByText('Critically high')).toBeTruthy()
  })

  it('asks for a preliminary phone call for a critical value before authorisation', async () => {
    const critical = result({ value_text: '7.0', abnormal_flag: 'HH', flag_text: 'Critically high',
      is_critical: 1, critical_limit_text: 'Critical above 6.5 mmol/L' })
    api.getValidationQueues.mockResolvedValue(queues({ technical: [group([critical])] }))
    render(<ValidateView {...props} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Critical result: Potassium 7.0 mmol/L')
    expect(alert.textContent).toContain('preliminary and not yet authorised')
  })

  it('shows the delta reason to the signatory', async () => {
    const explained = result({ status: 'tech_validated', value_text: '5.5', delta: FLAGGED_DELTA,
      delta_ack: { by_name: 'Tech A', at: '2026-10-09T04:10:00Z', reason: 'On potassium replacement' },
      may_authorise: true })
    api.getValidationQueues.mockResolvedValue(queues({ authorisation: [group([explained])],
      can_authorise: true }))
    render(<ValidateView {...props} />)
    expect(await screen.findByText(/Reason from Tech A/)).toBeTruthy()
    expect(screen.getByText(/On potassium replacement/)).toBeTruthy()
    // A signatory reads the reason; she does not give one here.
    expect(screen.queryByLabelText('Delta reason, Potassium')).toBeNull()
  })

  it('sends a result back only with a reason', async () => {
    const waiting = result({ status: 'tech_validated', may_authorise: true })
    api.getValidationQueues.mockResolvedValue(queues({ authorisation: [group([waiting])],
      can_authorise: true }))
    api.returnToBench.mockResolvedValue({ result: {} })
    render(<ValidateView {...props} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Send back' }))
    fireEvent.click(screen.getByRole('button', { name: 'Send back to the bench' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Say why')
    expect(api.returnToBench).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Why Potassium goes back'),
      { target: { value: 'Check for haemolysis' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send back to the bench' }))
    await waitFor(() => expect(api.returnToBench).toHaveBeenCalledWith(
      41, 'Check for haemolysis', { value_text: '4.4', comment: null, abnormal_flag: '' }))
  })

  it('reloads when a step is refused because the screen is out of date', async () => {
    const waiting = result({ status: 'tech_validated', may_authorise: true })
    api.getValidationQueues.mockResolvedValue(queues({ authorisation: [group([waiting])],
      can_authorise: true }))
    api.authoriseAndRelease.mockRejectedValue(new LabApiError(
      'You entered, changed or validated Potassium on this sample. This site requires a second person to authorise it. Nothing was released.',
      403, 'second_person_required'))
    render(<ValidateView {...props} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Authorise and release 1 result' }))
    expect(await screen.findByText(/requires a second person/)).toBeTruthy()
    expect(screen.getByText(/The lists have been reloaded/)).toBeTruthy()
    await waitFor(() => expect(api.getValidationQueues).toHaveBeenCalledTimes(2))
  })

  it('says the site lets one signatory do every step when it does', async () => {
    api.getValidationQueues.mockResolvedValue(queues({ require_distinct_authoriser: false }))
    render(<ValidateView {...props} />)
    expect(await screen.findByText(/lets one signatory do every step/)).toBeTruthy()
  })

  it('never shows a failed load as an empty list', async () => {
    api.getValidationQueues.mockRejectedValue(new LabApiError('Server error', 500, 'http_500'))
    render(<ValidateView {...props} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('This is a display failure, not an empty list.')
    expect(screen.queryByText(/No entered result is waiting/)).toBeNull()
  })

  it('hands a switched-off module to the panel', async () => {
    api.getValidationQueues.mockRejectedValue(new LabApiError('Off', 404, 'module_disabled'))
    render(<ValidateView {...props} />)
    await waitFor(() => expect(props.onModuleOff).toHaveBeenCalled())
  })
})

describe('ResultEntryGrid delta reason', () => {
  function item(over: Partial<WorklistItem> = {}): WorklistItem {
    return {
      item_id: 1,
      order_id: 'O-1',
      specimen_id: '261009000127',
      priority: 'routine',
      status: 'resulted',
      patient: ASHA,
      test: { id: 'LOINC:2823-3', name: 'Potassium', short_name: 'K', result_type: 'numeric',
        unit: 'mmol/L', decimal_places: 1, coded_values: [] },
      received_at: '2026-10-09T03:50:00Z',
      collected_at: '2026-10-09T03:30:00Z',
      tat_minutes: null,
      range: { id: 7, text: '3.5 to 5.1 mmol/L', configured: true },
      result: {
        id: 41, status: 'entered', version: 1, value_text: '5.5', comparator: null,
        unit: 'mmol/L', abnormal_flag: 'H', reference_range_id: 7,
        reference_range_text: '3.5 to 5.1 mmol/L', comment: null, entered_by_name: 'Tech A',
        entered_at: '2026-10-09T04:00:00Z', flag_text: 'High',
        delta: FLAGGED_DELTA, needs_delta_reason: true, delta_ack: null,
      } as WorklistItem['result'],
      ...over,
    }
  }

  it('asks for a reason under a flagged value and sends it with what the row showed', async () => {
    const onSaved = vi.fn()
    api.acknowledgeDelta.mockResolvedValue({
      result: { id: 41, status: 'entered', value_text: '5.5', comment: null, abnormal_flag: 'H',
        flag_text: 'High', delta: FLAGGED_DELTA, needs_delta_reason: false,
        delta_ack: { by_name: 'Tech A', at: '2026-10-09T04:05:00Z', reason: 'Known trend' } } as never,
      acknowledged: true })
    render(<ResultEntryGrid items={[item()]} onSaved={onSaved} />)
    const note = screen.getByLabelText('Delta check, Potassium for Asha Menon')
    expect(note.textContent).toContain('Delta flag.')
    expect(note.textContent).toContain('at or beyond the delta limit of 1 mmol/L')
    fireEvent.change(screen.getByLabelText('Delta reason, Potassium for Asha Menon'),
      { target: { value: 'Known trend' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save reason' }))
    await waitFor(() => expect(api.acknowledgeDelta).toHaveBeenCalledWith(
      41, 'Known trend', { value_text: '5.5', comment: null, abnormal_flag: 'H' }))
    expect(onSaved).toHaveBeenCalledWith('Potassium for Asha Menon: delta reason saved.')
    expect(await screen.findByText(/Reason from Tech A: Known trend/)).toBeTruthy()
    expect(screen.queryByLabelText('Delta reason, Potassium for Asha Menon')).toBeNull()
  })

  it('says nothing under a first value with no earlier result', () => {
    const first = item()
    first.result = { ...first.result, delta: { ...FLAGGED_DELTA, status: 'no_previous',
      note: 'No earlier result for this test, so there is nothing to compare.', previous: null },
    needs_delta_reason: false } as unknown as WorklistItem['result']
    render(<ResultEntryGrid items={[first]} onSaved={vi.fn()} />)
    expect(screen.queryByLabelText('Delta check, Potassium for Asha Menon')).toBeNull()
  })

  it('says who sent a result back and why', () => {
    const back = item()
    back.result = { ...back.result, delta: null, needs_delta_reason: false,
      returned: { by_name: 'Dr Rao', at: '2026-10-09T04:20:00Z', reason: 'Check for haemolysis' },
    } as unknown as WorklistItem['result']
    render(<ResultEntryGrid items={[back]} onSaved={vi.fn()} />)
    expect(screen.getByText(/Sent back to the bench by Dr Rao/).textContent).toContain(
      'Check for haemolysis')
  })
})
