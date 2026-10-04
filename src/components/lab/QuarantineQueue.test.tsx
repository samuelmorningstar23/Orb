/**
 * QuarantineQueue: the unmatched results queue (lis-19). What it must never
 * get wrong:
 *
 * 1. THE ADMINISTRATOR'S LIST IS READ ONLY and carries no patient identifier
 *    and no value (ruling LIS-R17): counts, arrival, sender, test, state.
 * 2. SWITCHED OFF AND EMPTY is said in words on the Laboratory screen, and is
 *    simply absent from the administrator's monitor.
 * 3. A REFUSED ATTACH IS SAID IDENTIFIER BY IDENTIFIER, and the form stays
 *    open; a refusal is never read as success.
 * 4. NOTHING IS SENT WITHOUT A HOSPITAL NUMBER AND A REASON.
 * 5. THE VIEW IS FOUND BY THE LAB PANEL and shown only to ingest.reconcile.
 * 6. EACH ROW SAYS WHETHER IT IS A FINAL, A CORRECTION OR A PRELIMINARY, so
 *    a stale final is never mistaken for its correction.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../api/lab'
import { resultStatusText, type QuarantineItemFull, type QuarantineList } from '../../api/lab/quarantine'
import { discoverViews, visibleViews } from '../../pages/panels/lab/viewRegistry'
import * as reconcileModule from '../../pages/panels/lab/ReconcileView'
import QuarantineQueue, { type QuarantineApi } from './QuarantineQueue'

const HELD: QuarantineItemFull = {
  id: 7,
  created_at: '2026-10-04T08:10:00.000000+00:00',
  lane: 'ingestion',
  status: 'open',
  source: 'ingested_hl7',
  sender: 'City Lab',
  observation_name: 'Potassium',
  observed_at: '2026-10-04T08:00:00+00:00',
  swept: false,
  resolved_at: null,
  reason: 'unmatched_patient',
  observation_code: '2823-3',
  value: '5.1',
  unit: 'mmol/L',
  abnormal_flag: 'H',
  reference_range: '3.5-5.1',
  result_status: 'F',
  message_identifiers: ['EXT123', 'ALT-9'],
  message_name: 'SHARMA, RAVI',
  message_date_of_birth: '1970-01-01',
  message_sex: 'M',
}

function listOf(items: QuarantineItemFull[], over: Partial<QuarantineList> = {}): QuarantineList {
  return {
    enabled: true,
    view: 'reconcile',
    status: 'open',
    counts: { open: items.length, attached: 4, discarded: 1, oldest_open_at: items[0]?.created_at ?? null },
    items,
    ...over,
  }
}

function makeApi(over: Partial<QuarantineApi> = {}): QuarantineApi {
  return {
    list: vi.fn().mockResolvedValue(listOf([HELD])),
    attach: vi.fn(),
    discard: vi.fn(),
    ...over,
  } as QuarantineApi
}

const OFF = new LabApiError('The unmatched results queue is switched off on this site.', 404,
  'feature_disabled', { error: 'feature_disabled', feature: 'result_quarantine' })

describe('QuarantineQueue, laboratory staff', () => {
  it('shows what the message said about the patient, the value and the sending laboratory flag', async () => {
    render(<QuarantineQueue mode="reconcile" api={makeApi()} />)
    expect(await screen.findByRole('heading', { name: /Potassium/ })).toHaveTextContent('Potassium 5.1 mmol/L')
    expect(screen.getByText('EXT123, ALT-9')).toBeInTheDocument()
    expect(screen.getByText('SHARMA, RAVI')).toBeInTheDocument()
    expect(screen.getByText('1 Jan 1970')).toBeInTheDocument()
    expect(screen.getByText(/Laboratory flag: High/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Attach to a patient' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Discard' })).toBeInTheDocument()
  })

  it('sends nothing without a hospital number or a reason', async () => {
    const api = makeApi()
    render(<QuarantineQueue mode="reconcile" api={api} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Attach to a patient' }))
    const form = screen.getByRole('form', { name: /Attach Potassium/ })
    fireEvent.submit(form)
    expect(await within(form).findByRole('alert')).toHaveTextContent("Type the patient's hospital number.")
    fireEvent.change(within(form).getByLabelText('Hospital number'), { target: { value: 'MRN-1' } })
    fireEvent.submit(form)
    expect(await within(form).findByRole('alert')).toHaveTextContent('Give a reason.')
    expect(api.attach).not.toHaveBeenCalled()
  })

  it('attaches with the identifiers typed, then says so and reloads the list', async () => {
    const api = makeApi({
      attach: vi.fn().mockResolvedValue({ attached: true, id: 7, patient_id: 'P1', observation_id: 99,
        superseded: [], checks: { name: 'match', date_of_birth: 'match' } }),
    })
    vi.mocked(api.list).mockResolvedValueOnce(listOf([HELD])).mockResolvedValueOnce(listOf([]))
    render(<QuarantineQueue mode="reconcile" api={api} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Attach to a patient' }))
    const form = screen.getByRole('form', { name: /Attach Potassium/ })
    fireEvent.change(within(form).getByLabelText('Hospital number'), { target: { value: ' MRN-1 ' } })
    fireEvent.change(within(form).getByLabelText('Full name'), { target: { value: 'Ravi Sharma' } })
    fireEvent.change(within(form).getByLabelText('Date of birth'), { target: { value: '1970-01-01' } })
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Request form checked' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Attach result' }))
    await waitFor(() => expect(api.attach).toHaveBeenCalledWith(7, {
      hospital_number: 'MRN-1', name: 'Ravi Sharma', date_of_birth: '1970-01-01',
      reason: 'Request form checked',
    }))
    expect(await screen.findByText("Potassium is now on the patient's chart.")).toBeInTheDocument()
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('Nothing is waiting.')).toBeInTheDocument()
  })

  it('says a refused attach identifier by identifier and keeps the form open', async () => {
    const api = makeApi({
      attach: vi.fn().mockRejectedValue(new LabApiError('These identifiers do not confirm the patient.',
        409, 'identity_not_confirmed', {
          error: 'identity_not_confirmed',
          checks: { hospital_number: 'differs_in_message', name: 'match', date_of_birth: 'no_match' },
        })),
    })
    render(<QuarantineQueue mode="reconcile" api={api} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Attach to a patient' }))
    const form = screen.getByRole('form', { name: /Attach Potassium/ })
    fireEvent.change(within(form).getByLabelText('Hospital number'), { target: { value: 'MRN-1' } })
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Checked' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Attach result' }))
    const checks = await within(form).findByRole('list', { name: 'Identifier checks' })
    expect(checks).toHaveTextContent('Hospital numberThe message has a different number, so it cannot count')
    expect(checks).toHaveTextContent('NameMatches the message and the record')
    expect(checks).toHaveTextContent('Date of birthDoes not match')
    expect(within(form).getByRole('alert')).toHaveTextContent('do not confirm the patient')
    expect(screen.queryByText(/is now on the patient's chart/)).not.toBeInTheDocument()
    expect(api.list).toHaveBeenCalledTimes(1)
  })

  it('says when a number in the message now belongs to a different patient, and keeps the form open', async () => {
    const api = makeApi({
      attach: vi.fn().mockRejectedValue(new LabApiError('A hospital number in this message now belongs to a different patient.',
        409, 'message_names_another_patient', {
          error: 'message_names_another_patient',
          checks: { hospital_number: 'names_another_patient', name: 'match', date_of_birth: 'match' },
        })),
    })
    render(<QuarantineQueue mode="reconcile" api={api} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Attach to a patient' }))
    const form = screen.getByRole('form', { name: /Attach Potassium/ })
    fireEvent.change(within(form).getByLabelText('Hospital number'), { target: { value: 'MRN-1' } })
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Checked' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Attach result' }))
    const checks = await within(form).findByRole('list', { name: 'Identifier checks' })
    expect(checks).toHaveTextContent("Hospital numberThe message's number now belongs to a different patient's record")
    expect(checks).toHaveTextContent('NameMatches the message and the record')
    expect(within(form).getByRole('alert')).toHaveTextContent("now belongs to a different patient's record")
    expect(within(form).getByRole('alert')).not.toHaveTextContent('do not confirm the patient')
    expect(screen.queryByText(/is now on the patient's chart/)).not.toBeInTheDocument()
    expect(api.list).toHaveBeenCalledTimes(1)
  })

  it('says when another record shares the name and date of birth, identifier by identifier', async () => {
    const api = makeApi({
      attach: vi.fn().mockRejectedValue(new LabApiError('Another patient record has the same name.',
        409, 'name_and_birth_date_shared', {
          error: 'name_and_birth_date_shared',
          checks: { hospital_number: 'differs_in_message', name: 'shared_with_another_patient',
            date_of_birth: 'shared_with_another_patient' },
        })),
    })
    render(<QuarantineQueue mode="reconcile" api={api} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Attach to a patient' }))
    const form = screen.getByRole('form', { name: /Attach Potassium/ })
    fireEvent.change(within(form).getByLabelText('Hospital number'), { target: { value: 'MRN-1' } })
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Checked' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Attach result' }))
    const checks = await within(form).findByRole('list', { name: 'Identifier checks' })
    expect(checks).toHaveTextContent("NameMatches, but another patient's record has the same name and date of birth")
    expect(within(form).getByRole('alert')).toHaveTextContent('can be attached only by a hospital number in the message')
    expect(screen.queryByText(/is now on the patient's chart/)).not.toBeInTheDocument()
  })

  it('says when a correction sent after this result is already on the chart', async () => {
    const api = makeApi({
      attach: vi.fn().mockRejectedValue(new LabApiError('A corrected result is on the chart.',
        409, 'later_correction_on_chart', {
          error: 'later_correction_on_chart',
          checks: { hospital_number: 'differs_in_message', name: 'match', date_of_birth: 'match' },
        })),
    })
    render(<QuarantineQueue mode="reconcile" api={api} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Attach to a patient' }))
    const form = screen.getByRole('form', { name: /Attach Potassium/ })
    fireEvent.change(within(form).getByLabelText('Hospital number'), { target: { value: 'MRN-1' } })
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Checked' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Attach result' }))
    expect(await within(form).findByRole('alert'))
      .toHaveTextContent('sent after this one, is already on the patient\'s chart')
    expect(api.list).toHaveBeenCalledTimes(1)
  })

  it('says when an attached result went on the chart behind a later correction', async () => {
    const api = makeApi({
      attach: vi.fn().mockResolvedValue({ attached: true, id: 7, patient_id: 'P1', observation_id: 99,
        superseded: [], superseded_by: 98, checks: { name: 'match', date_of_birth: 'match' } }),
    })
    render(<QuarantineQueue mode="reconcile" api={api} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Attach to a patient' }))
    const form = screen.getByRole('form', { name: /Attach Potassium/ })
    fireEvent.change(within(form).getByLabelText('Hospital number'), { target: { value: 'MRN-1' } })
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Checked' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Attach result' }))
    expect(await screen.findByText(
      "Potassium is now on the patient's chart, behind the corrected result the laboratory sent after it.",
    )).toBeInTheDocument()
  })

  it('shows on each row whether it is a final, a correction or a preliminary', async () => {
    const corrected: QuarantineItemFull = { ...HELD, id: 8, result_status: 'C', value: '4.2' }
    const prelim: QuarantineItemFull = { ...HELD, id: 9, result_status: 'P', value: '6.0' }
    render(<QuarantineQueue mode="reconcile" api={makeApi({
      list: vi.fn().mockResolvedValue(listOf([corrected, HELD, prelim])) })} />)
    const rows = await screen.findAllByRole('listitem')
    expect(rows[0]).toHaveTextContent('Corrected result.')
    expect(rows[1]).toHaveTextContent('Final result.')
    expect(rows[2]).toHaveTextContent('Preliminary result.')
  })

  it('discards only with a reason', async () => {
    const api = makeApi({ discard: vi.fn().mockResolvedValue({ discarded: true, id: 7 }) })
    render(<QuarantineQueue mode="reconcile" api={api} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Discard' }))
    const form = screen.getByRole('form', { name: /Discard Potassium/ })
    fireEvent.submit(form)
    expect(await within(form).findByRole('alert')).toHaveTextContent('Give a reason.')
    expect(api.discard).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Not our patient' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Discard result' }))
    await waitFor(() => expect(api.discard).toHaveBeenCalledWith(7, 'Not our patient'))
    expect(await screen.findByText('Potassium was discarded.')).toBeInTheDocument()
  })

  it('says the queue is switched off when the server says so, never an empty list', async () => {
    render(<QuarantineQueue mode="reconcile" api={makeApi({ list: vi.fn().mockRejectedValue(OFF) })} />)
    expect(await screen.findByText(/switched off on this site, and nothing is waiting/)).toBeInTheDocument()
    expect(screen.queryByText('Nothing is waiting.')).not.toBeInTheDocument()
  })

  it('switched off with results left, it says they still need a decision', async () => {
    render(<QuarantineQueue mode="reconcile" api={makeApi({
      list: vi.fn().mockResolvedValue(listOf([HELD], { enabled: false })) })} />)
    expect(await screen.findByText(/switched off for new results/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Attach to a patient' })).toBeInTheDocument()
  })

  it('switched off with nothing in it, it says results are stored as before', async () => {
    const empty = listOf([], { enabled: false,
      counts: { open: 0, attached: 0, discarded: 0, oldest_open_at: null } })
    render(<QuarantineQueue mode="reconcile" api={makeApi({ list: vi.fn().mockResolvedValue(empty) })} />)
    expect(await screen.findByText(/switched off on this site\. A result that matches no patient is stored without one/))
      .toBeInTheDocument()
    expect(screen.queryByText(/still need a decision/)).not.toBeInTheDocument()
  })

  it('offers only Discard, and says why, for a message that names the patient one way only', async () => {
    const oneWay: QuarantineItemFull = { ...HELD, identifiable: false, message_name: null,
      message_date_of_birth: null, message_identifiers: ['EXT123'] }
    render(<QuarantineQueue mode="reconcile" api={makeApi({
      list: vi.fn().mockResolvedValue(listOf([oneWay])) })} />)
    expect(await screen.findByText(/names the patient in only one way, so it cannot be attached/))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Attach to a patient' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Discard' })).toBeInTheDocument()
  })

  it('offers a retry when the list cannot be loaded, and filters by state', async () => {
    const list = vi.fn()
      .mockRejectedValueOnce(new LabApiError('Could not reach the laboratory.', 0, 'network_error'))
      .mockResolvedValue(listOf([HELD]))
    render(<QuarantineQueue mode="reconcile" api={makeApi({ list })} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the laboratory.')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('heading', { name: /Potassium/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Attached' }))
    await waitFor(() => expect(list).toHaveBeenLastCalledWith('attached'))
  })

  it('shows who decided a resolved result and why, with no actions', async () => {
    const done: QuarantineItemFull = { ...HELD, status: 'discarded', resolved_by_name: 'Meena Iyer',
      resolved_at: '2026-10-04T09:00:00+00:00', resolution_reason: 'Resent with the right number' }
    render(<QuarantineQueue mode="reconcile" api={makeApi({
      list: vi.fn().mockResolvedValue(listOf([done])) })} />)
    expect(await screen.findByText(/Discarded by Meena Iyer on .*: Resent with the right number/))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Attach to a patient' })).not.toBeInTheDocument()
  })
})

describe('resultStatusText', () => {
  it('names HL7 and feed statuses in words and keeps an unknown code as sent', () => {
    expect(resultStatusText('F')).toBe('Final result')
    expect(resultStatusText('final')).toBe('Final result')
    expect(resultStatusText('C')).toBe('Corrected result')
    expect(resultStatusText('corrected')).toBe('Corrected result')
    expect(resultStatusText('P')).toBe('Preliminary result')
    expect(resultStatusText('Q')).toBe('Result status Q')
    expect(resultStatusText('  ')).toBeNull()
    expect(resultStatusText(null)).toBeNull()
  })
})

describe('QuarantineQueue, the administrator', () => {
  it('is read only, with counts and no identifiers or values', async () => {
    const minimal = {
      id: 7, created_at: HELD.created_at, lane: 'ingestion' as const, status: 'open' as const,
      source: 'ingested_hl7', sender: 'City Lab', observation_name: 'Potassium',
      observed_at: HELD.observed_at, swept: false, resolved_at: null,
    }
    render(<QuarantineQueue mode="monitor" api={makeApi({
      list: vi.fn().mockResolvedValue(listOf([minimal], { view: 'monitor' })) })} />)
    const table = await screen.findByRole('table')
    expect(within(table).getByText('Potassium')).toBeInTheDocument()
    expect(within(table).getByText('City Lab')).toBeInTheDocument()
    expect(within(table).getByText('Waiting')).toBeInTheDocument()
    expect(screen.getByText(/This list is read only/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Attach to a patient' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Discard' })).not.toBeInTheDocument()
    expect(screen.queryByText('EXT123')).not.toBeInTheDocument()
    expect(screen.queryByText(/5\.1/)).not.toBeInTheDocument()
  })

  it('renders nothing at all when the queue is off and empty', async () => {
    const list = vi.fn().mockRejectedValue(OFF)
    const { container } = render(<QuarantineQueue mode="monitor" api={makeApi({ list })} />)
    await waitFor(() => expect(list).toHaveBeenCalled())
    await Promise.resolve()
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the server answers that the queue is off with nothing in it', async () => {
    const list = vi.fn().mockResolvedValue(listOf([], { view: 'monitor', enabled: false,
      counts: { open: 0, attached: 0, discarded: 0, oldest_open_at: null } }))
    const { container } = render(<QuarantineQueue mode="monitor" api={makeApi({ list })} />)
    await waitFor(() => expect(list).toHaveBeenCalled())
    await Promise.resolve()
    expect(container).toBeEmptyDOMElement()
  })
})

describe('ReconcileView', () => {
  it('is found by the laboratory panel and shown only to ingest.reconcile', () => {
    const views = discoverViews({ './lab/ReconcileView.tsx': reconcileModule })
    expect(views).toHaveLength(1)
    expect(views[0]).toMatchObject({ id: 'reconcile', label: 'Unmatched results', order: 88,
      permission: 'ingest.reconcile', whenDraining: 'show' })
    expect(visibleViews(views, (p) => p === 'ingest.reconcile')).toHaveLength(1)
    expect(visibleViews(views, (p) => p === 'lab.read')).toHaveLength(0)
    expect(visibleViews(views, (p) => p === 'ingest.reconcile', 'draining')).toHaveLength(1)
  })
})
