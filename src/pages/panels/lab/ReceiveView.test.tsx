/**
 * The Receive view (lis-9). What it must never get wrong:
 *
 *   · A misread check digit is refused in the server's words and opens no
 *     sample.
 *   · It draws only the steps the server offers for the sample in hand.
 *   · A sample past its stability time is received only with a tick and a
 *     reason; 'Delayed in transit' is the suggested rejection.
 *   · A rejection needs a reason (and a note for Other); the answer says
 *     whether the ward was told, and shows the specimen error prompt when the
 *     server sends one.
 *   · A rejected sample offers recollect or cancel, and recollect is not
 *     offered while the module drains.
 *   · A failed load of the lists is never an empty list.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../../api/lab'
import type { ReceiptSpecimen, ReceivingLists } from '../../../api/lab/receipt'
import ReceiveView, * as receiveModule from './ReceiveView'
import { discoverViews } from './viewRegistry'

vi.mock('../../../api/lab/receipt', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/receipt')>(
    '../../../api/lab/receipt')
  return {
    ...actual,
    getReceivingLists: vi.fn(),
    scanSpecimen: vi.fn(),
    receiveSpecimen: vi.fn(),
    rejectSpecimen: vi.fn(),
    recollectSpecimen: vi.fn(),
    cancelRejectedTests: vi.fn(),
  }
})

const api = vi.mocked(await import('../../../api/lab/receipt'))

const ASHA = { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', bed: '4A', ward: 'GM-1',
  department: 'General Medicine' }

const REASONS = [
  { code: 'clotted', display: 'Clotted', incident_prompt: false },
  { code: 'delayed', display: 'Delayed in transit', incident_prompt: false },
  { code: 'mislabelled', display: 'Mislabelled', incident_prompt: true },
  { code: 'other', display: 'Other', incident_prompt: false },
]

function specimen(over: Partial<ReceiptSpecimen> = {}): ReceiptSpecimen {
  return {
    id: '261006000127',
    status: 'collected',
    status_text: 'Collected, not yet received',
    priority: 'routine',
    patient: ASHA,
    specimen_type_code: 'serum',
    specimen_display: 'Serum',
    container: 'Gold top',
    container_colour_name: 'gold',
    collected_at: '2026-10-06T04:00:00.000000+00:00',
    collected_by_name: 'Nurse A',
    received_at: null,
    received_by_name: null,
    receipt_note: null,
    rejected_at: null,
    rejected_by_name: null,
    rejection_code: null,
    rejection_display: null,
    rejection_note: null,
    recollect_of: null,
    recollected_as: [],
    tests: [{ item_id: 1, order_id: 'O-1', test_id: 'LOINC:2823-3', name: 'Potassium',
      short_name: 'K', status: 'collected', status_text: 'Collected', section_id: 'biochemistry',
      cancelled: null }],
    transit_minutes: 12,
    stability: { stability_minutes: null, age_minutes: 12, exceeded: false, suggested_rejection: null },
    actions: { receive: true, reject: true, recollect: false, cancel_rejected: false },
    ...over,
  }
}

function lists(over: Partial<ReceivingLists> = {}): ReceivingLists {
  return { in_transit: [], rejected: [], rejection_reasons: REASONS,
    scope: { department: null, hospital_wide: true }, ...over }
}

const props = { meta: {}, state: 'on' as const, openView: vi.fn(), onModuleOff: vi.fn() }

beforeEach(() => {
  api.getReceivingLists.mockResolvedValue(lists())
})
afterEach(() => {
  vi.clearAllMocks()
})

async function scan(code: string) {
  const box = await screen.findByLabelText('Accession number')
  fireEvent.change(box, { target: { value: code } })
  fireEvent.click(screen.getByRole('button', { name: 'Find sample' }))
}

describe('ReceiveView', () => {
  it('declares itself to the panel as the receive tab', () => {
    const [entry] = discoverViews({ './lab/ReceiveView.tsx': receiveModule })
    expect(entry).toMatchObject({ id: 'receive', label: 'Receive', order: 20,
      permission: 'lab.receive', whenDraining: 'show' })
  })

  it('lists samples in transit and rejected samples with their reason', async () => {
    api.getReceivingLists.mockResolvedValue(lists({
      in_transit: [{ ...specimen({ priority: 'stat' }), tests: [{ item_id: 1, order_id: 'O-1',
        test_id: 'LOINC:2823-3', status: 'collected', name: 'Potassium', short_name: 'K' }] }],
      rejected: [{ ...specimen({ id: '261006000135', status: 'rejected', rejection_code: 'clotted',
        rejection_display: 'Clotted', rejection_note: 'Small clot',
        rejected_at: '2026-10-06T05:00:00.000000+00:00', rejected_by_name: 'Tech B' }),
        tests: [] }],
    }))
    render(<ReceiveView {...props} />)
    const transit = await screen.findByRole('region', { name: /In transit \(1\)/ })
    expect(within(transit).getByText('STAT')).toBeTruthy()
    expect(within(transit).getByText('12 min')).toBeTruthy()
    const rejected = screen.getByRole('region', { name: /Rejected, waiting for a decision \(1\)/ })
    expect(within(rejected).getByText('Clotted: Small clot')).toBeTruthy()
  })

  it('refuses a misread check digit in the server\'s words and opens nothing', async () => {
    api.scanSpecimen.mockRejectedValue(new LabApiError(
      'The check digit does not match. Scan the label again, or check each digit you typed.',
      422, 'bad_accession'))
    render(<ReceiveView {...props} />)
    await scan('261006000128')
    expect((await screen.findByRole('alert')).textContent).toContain('The check digit does not match')
    expect(screen.queryByRole('button', { name: 'Receive' })).toBeNull()
  })

  it('receives a scanned sample and refreshes the lists', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen() })
    api.receiveSpecimen.mockResolvedValue({
      specimen: specimen({ status: 'received', status_text: 'Received in the laboratory',
        actions: { receive: false, reject: true, recollect: false, cancel_rejected: false } }),
      items_received: 1, orders: [] })
    render(<ReceiveView {...props} />)
    await scan('261006 00012 7')
    expect(api.scanSpecimen).toHaveBeenCalledWith('261006 00012 7')
    fireEvent.click(await screen.findByRole('button', { name: 'Receive' }))
    await waitFor(() => expect(api.receiveSpecimen).toHaveBeenCalledWith('261006000127', {}))
    expect(await screen.findByText(/received: 1 test on the worklists/)).toBeTruthy()
    expect(api.getReceivingLists).toHaveBeenCalledTimes(2)
  })

  it('receives a sample past its stability time only with a tick and a reason', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen({ stability: {
      stability_minutes: 60, age_minutes: 180, exceeded: true, suggested_rejection: 'delayed' } }) })
    api.receiveSpecimen.mockResolvedValue({ specimen: specimen({ status: 'received' }),
      items_received: 1, orders: [] })
    render(<ReceiveView {...props} />)
    await scan('261006000127')
    expect(await screen.findByText('Past its stability time')).toBeTruthy()
    const receive = screen.getByRole('button', { name: 'Receive' }) as HTMLButtonElement
    expect(receive.disabled).toBe(true)
    fireEvent.click(screen.getByLabelText('Process it anyway'))
    expect(receive.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Why it is being processed'),
      { target: { value: 'Pathologist asked' } })
    expect(receive.disabled).toBe(false)
    fireEvent.click(receive)
    await waitFor(() => expect(api.receiveSpecimen).toHaveBeenCalledWith('261006000127',
      { accept_past_stability: true, receipt_note: 'Pathologist asked' }))
  })

  it('suggests delayed in transit for a sample past its stability time', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen({ stability: {
      stability_minutes: 60, age_minutes: 180, exceeded: true, suggested_rejection: 'delayed' } }) })
    render(<ReceiveView {...props} />)
    await scan('261006000127')
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    expect((screen.getByLabelText('Reason') as HTMLSelectElement).value).toBe('delayed')
  })

  it('needs a reason to reject, and a note for other', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen() })
    render(<ReceiveView {...props} />)
    await scan('261006000127')
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    const confirm = screen.getByRole('button', { name: 'Reject sample' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'other' } })
    expect(confirm.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('What was wrong'), { target: { value: 'Tube cracked' } })
    expect(confirm.disabled).toBe(false)
    expect(api.rejectSpecimen).not.toHaveBeenCalled()
  })

  it('says when the ward could not be told and shows the specimen error prompt', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen() })
    api.rejectSpecimen.mockResolvedValue({
      specimen: specimen({ status: 'rejected', status_text: 'Rejected', rejection_code: 'mislabelled',
        rejection_display: 'Mislabelled', rejected_at: '2026-10-06T05:00:00.000000+00:00',
        actions: { receive: false, reject: false, recollect: true, cancel_rejected: true } }),
      rejected_items: 1, withdrawn_results: 0, orders: [],
      incident_prompt: { incident_type: 'specimen_error', reason_code: 'mislabelled',
        reason: 'Mislabelled', message: 'Report it as a specimen error in the incident register.' },
      ward_notice: { posted: false, reason: 'no_case_room' },
    })
    render(<ReceiveView {...props} />)
    await scan('261006000127')
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'mislabelled' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reject sample' }))
    await waitFor(() => expect(api.rejectSpecimen).toHaveBeenCalledWith('261006000127',
      { reason_code: 'mislabelled' }))
    expect(await screen.findByText(/this patient has no case room. Phone the ward/)).toBeTruthy()
    expect(screen.getByText('Report a specimen error')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Recollect' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Cancel these tests' })).toBeTruthy()
  })

  it('tells the laboratory to phone the ward when the patient has been discharged', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen() })
    api.rejectSpecimen.mockResolvedValue({
      specimen: specimen({ status: 'rejected', status_text: 'Rejected', rejection_code: 'clotted',
        rejection_display: 'Clotted', rejected_at: '2026-10-06T05:00:00.000000+00:00',
        actions: { receive: false, reject: false, recollect: true, cancel_rejected: true } }),
      rejected_items: 1, withdrawn_results: 0, orders: [], incident_prompt: null,
      ward_notice: { posted: false, reason: 'case_room_closed' },
    })
    render(<ReceiveView {...props} />)
    await scan('261006000127')
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'clotted' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reject sample' }))
    await waitFor(() => expect(api.rejectSpecimen).toHaveBeenCalledWith('261006000127',
      { reason_code: 'clotted' }))
    expect(await screen.findByText(
      /this patient has been discharged, so the case room is closed. Phone the ward/)).toBeTruthy()
    expect(screen.queryByText(/The ward was told in the patient's case room/)).toBeNull()
  })

  it('recollects a rejected sample and names the new tube', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen({ status: 'rejected',
      actions: { receive: false, reject: false, recollect: true, cancel_rejected: true } }) })
    api.recollectSpecimen.mockResolvedValue({
      specimen: specimen({ id: '261006000143', status: 'planned', recollect_of: '261006000127',
        actions: { receive: false, reject: false, recollect: false, cancel_rejected: false } }),
      recollect_of: '261006000127', recollected_as: '261006000143', items_moved: 1, orders: [] })
    render(<ReceiveView {...props} />)
    await scan('261006000127')
    fireEvent.click(await screen.findByRole('button', { name: 'Recollect' }))
    await waitFor(() => expect(api.recollectSpecimen).toHaveBeenCalledWith('261006000127'))
    expect(await screen.findByText(/A new sample 261006 00014 3 is planned/)).toBeTruthy()
  })

  it('cancels the rejected tests only with a reason', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen({ status: 'rejected',
      actions: { receive: false, reject: false, recollect: true, cancel_rejected: true } }) })
    api.cancelRejectedTests.mockResolvedValue({ specimen: specimen({ status: 'rejected',
      actions: { receive: false, reject: false, recollect: false, cancel_rejected: false } }),
    cancelled_items: 1, orders: [] })
    render(<ReceiveView {...props} />)
    await scan('261006000127')
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel these tests' }))
    const confirm = screen.getByRole('button', { name: 'Cancel tests' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Why they are cancelled instead of recollected'),
      { target: { value: 'Doctor no longer needs it' } })
    fireEvent.click(confirm)
    await waitFor(() => expect(api.cancelRejectedTests).toHaveBeenCalledWith('261006000127',
      'Doctor no longer needs it'))
  })

  it('does not offer recollect while the module drains', async () => {
    api.scanSpecimen.mockResolvedValue({ specimen: specimen({ status: 'rejected',
      actions: { receive: false, reject: false, recollect: true, cancel_rejected: true } }) })
    render(<ReceiveView {...props} state="draining" />)
    await scan('261006000127')
    expect(await screen.findByRole('button', { name: 'Cancel these tests' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Recollect' })).toBeNull()
    expect(screen.getByText(/New collections have stopped/)).toBeTruthy()
  })

  it('never shows a failed load as an empty list', async () => {
    api.getReceivingLists.mockRejectedValue(new LabApiError('Server error', 500, 'http_500'))
    render(<ReceiveView {...props} />)
    expect((await screen.findByRole('alert')).textContent).toContain('not an empty list')
    expect(screen.queryByText(/No collected sample is waiting/)).toBeNull()
  })

  it('hands the switched-off answer to the panel', async () => {
    const onModuleOff = vi.fn()
    api.getReceivingLists.mockRejectedValue(new LabApiError('off', 404, 'module_disabled'))
    render(<ReceiveView {...props} onModuleOff={onModuleOff} />)
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })
})
