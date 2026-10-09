/**
 * The front desk's OPD queue with the 'opd' feature on (opd-8).
 *
 *  1. The server marks its queue answer opd_mode; the desk then shows each
 *     visit's priority (with its reason), room and skipped state, and offers
 *     "Priority and room" and "Left without being seen". Without the mark the
 *     queue is exactly the old one.
 *  2. Left without being seen is a cancel with the LWBS code.
 *  3. A priority other than normal needs a reason before it can be saved.
 *  4. The desk offers Call in only to an opd.consult holder, and never
 *     Complete (finishing is on the doctor's Outpatients tab).
 *  5. QueueView.tsx is the built-in queue's body, never a discovered tab.
 *  6. Live desk nudges (opd-9b): the queue re-reads on 'opd:queue_changed'
 *     and on reconnecting, and stops its timed re-read while the socket is up
 *     and the server says this account hears the nudge; otherwise it keeps it.
 *     A quiet queue is then not re-read, so the waits keep running on screen
 *     from the last read until the next one replaces them.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetFeatureFlags, setFeatureFlags } from '../../../shellConfig'
import FrontDeskPanel from '../FrontDeskPanel'

const apiFetch = vi.fn()

/* A stand-in socket: handlers by event, and fire() to play the server. */
const sock = vi.hoisted(() => {
  const handlers: Record<string, Set<(...args: unknown[]) => void>> = {}
  return {
    connected: true,
    on(event: string, fn: (...args: unknown[]) => void) {
      (handlers[event] ??= new Set()).add(fn)
    },
    off(event: string, fn: (...args: unknown[]) => void) {
      handlers[event]?.delete(fn)
    },
    fire(event: string, ...args: unknown[]) {
      handlers[event]?.forEach(fn => fn(...args))
    },
    reset() {
      for (const key of Object.keys(handlers)) delete handlers[key]
    },
  }
})

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  socket: sock,
}))

const DEPT = 'Cardiology'

function visit(id: string, token: number, extra: Record<string, unknown> = {}) {
  return {
    id, patient_id: `P-${id}`, patient_name: `Patient ${id}`, patient_mrn: `MRN-${id}`,
    patient_age: 54, patient_sex: 'F', department: DEPT, doctor_id: 'DOC-1',
    doctor_name: 'Dr Iyer', visit_date: '2026-10-05', token_number: token, visit_type: 'new',
    chief_complaint: 'cough', status: 'waiting', registered_at: '2026-10-05T03:00:00+00:00',
    called_at: null, completed_at: null, cancelled_at: null, cancel_reason: null,
    consultation_charge_id: null, notes: null, waiting_minutes: 5, consultation_minutes: null,
    priority: 'normal', priority_label: 'Normal', priority_reason: null, room: null,
    call_count: 0, skipped: false, skipped_at: null, cancel_reason_code: null,
    cancel_reason_label: null, disposition: null, disposition_at: null,
    ...extra,
  }
}

const OPD_QUEUE = {
  visit_date: '2026-10-05',
  department: DEPT,
  queues: [{
    department: DEPT, doctor_id: 'DOC-1', doctor_name: 'Dr Iyer',
    visits: [
      visit('V-EMG', 4, { patient_name: 'Anjali Rao', priority: 'emergency',
        priority_label: 'Emergency', priority_reason: 'Chest pain', room: '2' }),
      visit('V-SKIP', 1, { patient_name: 'Bina Skipped', skipped: true,
        skipped_at: '2026-10-05T03:30:00+00:00' }),
      visit('V-IN', 2, { patient_name: 'Chand In', status: 'in_consultation' }),
    ],
    waiting_count: 2, in_consultation_count: 1, longest_wait_minutes: 5,
  }],
  total: 3,
  opd_mode: true,
  poll_seconds: 30,
  priorities: [{ code: 'normal', label: 'Normal' }, { code: 'emergency', label: 'Emergency' },
    { code: 'senior_citizen', label: 'Senior citizen' }],
  cancel_reasons: [{ code: 'left_without_being_seen', label: 'Left without being seen' }],
}

function routeApi(queue: unknown = OPD_QUEUE) {
  apiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith('/frontdesk/opd/queue')) return queue
    if (path.startsWith('/frontdesk/opd/doctors')) {
      return { doctors: [{ id: 'DOC-1', name: 'Dr Iyer', department: DEPT }] }
    }
    if (path.startsWith('/vigil/patients')) return []
    if (path.startsWith('/frontdesk/transfers/destinations')) return { destinations: [] }
    if (path.startsWith('/frontdesk/opd/visits/')) return visit('V-EMG', 4)
    throw new Error(`unrouted ${path}`)
  })
}

const receptionistHolds = (p: string) => ['registration.read', 'registration.write'].includes(p)
const adminHolds = (p: string) => ['patient.read_all', 'frontdesk.write', 'registration.read'].includes(p)
const doctorHolds = (p: string) => ['patient.read_all', 'patient.write', 'opd.consult'].includes(p)

function calls(method: string) {
  return apiFetch.mock.calls
    .filter(([, o]) => o && (o as RequestInit).method === method)
    .map(([path, o]) => [path as string, JSON.parse(String((o as RequestInit).body ?? 'null'))])
}

beforeEach(() => {
  apiFetch.mockReset()
  resetFeatureFlags()
  setFeatureFlags({ registration: true, opd: true })
})

afterEach(() => {
  cleanup()
  resetFeatureFlags()
  sock.reset()
  sock.connected = true
  vi.useRealTimers()
})

describe('the OPD-mode desk queue', () => {
  it('shows priority with its reason, room and skipped, and no Complete', async () => {
    routeApi()
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    const row = screen.getByText('Anjali Rao').closest('tr') as HTMLElement
    expect(within(row).getByText('Emergency')).toBeInTheDocument()
    expect(within(row).getByText('Chest pain')).toBeInTheDocument()
    expect(within(row).getByText('2')).toBeInTheDocument()
    const skipped = screen.getByText('Bina Skipped').closest('tr') as HTMLElement
    expect(within(skipped).getByText(/Skipped, not answered when called/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Call in' })).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Left without being seen' })).toHaveLength(2)
  })

  it('says when a doctor took a patient under a new token', async () => {
    const taken = visit('V-TAKEN', 3, { patient_name: 'Dev Taken', previous_token_number: 1 })
    routeApi({ ...OPD_QUEUE, queues: [{ ...OPD_QUEUE.queues[0],
      visits: [...OPD_QUEUE.queues[0].visits, taken] }] })
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Dev Taken')
    const row = screen.getByText('Dev Taken').closest('tr') as HTMLElement
    expect(within(row).getByText('Slip says token 1. Tell the patient their new token.')).toBeInTheDocument()
    expect(screen.getAllByText(/Slip says token/)).toHaveLength(1)
  })

  it('records left without being seen as a coded cancel', async () => {
    routeApi()
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    const row = screen.getByText('Anjali Rao').closest('tr') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Left without being seen' }))
    await waitFor(() => expect(calls('POST')).toContainEqual([
      '/frontdesk/opd/visits/V-EMG/cancel',
      { reason: 'Left without being seen', reason_code: 'left_without_being_seen' },
    ]))
  })

  it('needs a reason for a priority other than normal', async () => {
    routeApi()
    render(<FrontDeskPanel holds={adminHolds} />)
    await screen.findByText('Bina Skipped')
    const row = screen.getByText('Bina Skipped').closest('tr') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Priority and room' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Priority'), { target: { value: 'senior_citizen' } })
    const save = within(dialog).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Aged 84' } })
    fireEvent.change(within(dialog).getByLabelText('Room'), { target: { value: '5' } })
    fireEvent.click(save)
    await waitFor(() => expect(calls('PATCH')).toContainEqual([
      '/frontdesk/opd/visits/V-SKIP',
      { priority: 'senior_citizen', priority_reason: 'Aged 84', room: '5' },
    ]))
  })

  it('offers Call in to a consulting doctor only', async () => {
    routeApi()
    render(<FrontDeskPanel holds={doctorHolds} />)
    await screen.findByText('Anjali Rao')
    const row = screen.getByText('Anjali Rao').closest('tr') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Call in' }))
    await waitFor(() => expect(calls('POST').map(([p]) => p)).toContain('/frontdesk/opd/visits/V-EMG/call'))
    expect(screen.queryByRole('button', { name: 'Complete' })).toBeNull()
  })

  it('without the opd_mode mark the queue is the old one', async () => {
    const { opd_mode: _mark, ...plain } = OPD_QUEUE
    void _mark
    routeApi({ ...plain, queues: [{ ...plain.queues[0], visits: [plain.queues[0].visits[0]] }] })
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    expect(screen.queryByRole('columnheader', { name: 'Priority' })).toBeNull()
    expect(screen.getByRole('button', { name: 'No-show' })).toBeInTheDocument()
  })

  it('is never offered as a desk tab of its own', async () => {
    routeApi()
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    const tabs = screen.getAllByRole('tab').map(t => t.textContent)
    expect(tabs).not.toContain('Queue')
    expect(tabs[0]).toBe('OPD queue')
  })
})

describe('live desk nudges (opd-9b)', () => {
  const queueReads = () => apiFetch.mock.calls
    .filter(([p]) => String(p).startsWith('/frontdesk/opd/queue')).length

  it('re-reads on the nudge and runs no timed re-read while the socket is up', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    routeApi({ ...OPD_QUEUE, live_updates: true })
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    const before = queueReads()
    await act(async () => { await vi.advanceTimersByTimeAsync(95_000) })
    expect(queueReads()).toBe(before)

    // Three changes in a burst: one re-read. The payload is never read.
    await act(async () => {
      sock.fire('opd:queue_changed', { department: DEPT })
      sock.fire('opd:queue_changed', { department: DEPT })
      sock.fire('opd:queue_changed', { department: DEPT })
      await vi.advanceTimersByTimeAsync(500)
    })
    await waitFor(() => expect(queueReads()).toBe(before + 1))
  })

  it('keeps the timed re-read when the server says this account does not hear the nudge',
    async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      routeApi({ ...OPD_QUEUE, live_updates: false })
      render(<FrontDeskPanel holds={receptionistHolds} />)
      await screen.findByText('Anjali Rao')
      const before = queueReads()
      await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
      await waitFor(() => expect(queueReads()).toBeGreaterThan(before))
    })

  it('keeps the timed re-read while the socket is down, and reads once on reconnecting',
    async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      sock.connected = false
      routeApi({ ...OPD_QUEUE, live_updates: true })
      render(<FrontDeskPanel holds={receptionistHolds} />)
      await screen.findByText('Anjali Rao')
      const before = queueReads()
      await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
      await waitFor(() => expect(queueReads()).toBeGreaterThan(before))

      const down = queueReads()
      sock.connected = true
      await act(async () => { sock.fire('connect') })
      await waitFor(() => expect(queueReads()).toBe(down + 1))
      await act(async () => { await vi.advanceTimersByTimeAsync(95_000) })
      expect(queueReads()).toBe(down + 1)
    })

  const waitIn = (name: string) =>
    (screen.getByText(name).closest('tr') as HTMLElement).querySelector('.fd-wait') as HTMLElement
  const longestWait = () => screen.getByText('longest wait').previousElementSibling as HTMLElement

  it('keeps the waits running on screen while the queue is quiet', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    routeApi({ ...OPD_QUEUE, live_updates: true })
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    const before = queueReads()
    expect(waitIn('Anjali Rao')).toHaveTextContent(/^5 min$/)
    expect(longestWait()).toHaveTextContent(/^5 min$/)

    // Read at 09:05 with a 5 minute wait, nothing changes, it is now 09:50.
    await act(async () => { await vi.advanceTimersByTimeAsync(45 * 60_000) })
    expect(queueReads()).toBe(before)
    expect(waitIn('Anjali Rao')).toHaveTextContent(/^50 min$/)
    expect(waitIn('Anjali Rao')).toHaveClass('fd-wait--warn')
    expect(waitIn('Bina Skipped')).toHaveTextContent(/^50 min$/)
    expect(longestWait()).toHaveTextContent(/^50 min$/)
    // The patient in the room stopped waiting when called: that figure stays.
    expect(waitIn('Chand In')).toHaveTextContent(/^5 min$/)
  })

  it('a fresh read replaces the counted waits with the server\'s own', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    routeApi({ ...OPD_QUEUE, live_updates: true })
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000) })
    expect(waitIn('Anjali Rao')).toHaveTextContent(/^15 min$/)

    const [doctorQueue] = OPD_QUEUE.queues
    routeApi({
      ...OPD_QUEUE,
      live_updates: true,
      queues: [{
        ...doctorQueue,
        visits: doctorQueue.visits.map(v => (v.status === 'waiting' ? { ...v, waiting_minutes: 16 } : v)),
        longest_wait_minutes: 16,
      }],
    })
    await act(async () => {
      sock.fire('opd:queue_changed', { department: DEPT })
      await vi.advanceTimersByTimeAsync(500)
    })
    await waitFor(() => expect(waitIn('Anjali Rao')).toHaveTextContent(/^16 min$/))
    expect(longestWait()).toHaveTextContent(/^16 min$/)
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(waitIn('Anjali Rao')).toHaveTextContent(/^17 min$/)
  })

  it('keeps an unmeasured wait unknown however long the queue is quiet', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const [doctorQueue] = OPD_QUEUE.queues
    routeApi({
      ...OPD_QUEUE,
      live_updates: true,
      queues: [{
        ...doctorQueue,
        visits: doctorQueue.visits.map(v => ({ ...v, waiting_minutes: null })),
        longest_wait_minutes: null,
      }],
    })
    render(<FrontDeskPanel holds={receptionistHolds} />)
    await screen.findByText('Anjali Rao')
    await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
    expect(waitIn('Anjali Rao')).toHaveTextContent(/^Unknown$/)
    expect(longestWait()).toHaveTextContent(/^Unknown$/)
  })
})
