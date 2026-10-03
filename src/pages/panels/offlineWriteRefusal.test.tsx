/**
 * THE LAST TWO WRITES THAT STILL PRETENDED TO SUCCEED OFFLINE.
 *
 * Orb's offline mode is read-only on purpose (see lib/offline/writeGuard): a
 * clinician keeps her charts in a dead zone, but nothing can be recorded until
 * she is back, and she is TOLD so. Three surfaces already refuse outright —
 * observations, doses, handovers.
 *
 * Two did not. Both route through api/client.offlineMutate, which with no
 * connection writes the request into a localStorage queue and resolves with an
 * optimistic result, so the screen says the work landed:
 *
 *   RELAY       the message appears in the thread, indistinguishable from sent.
 *   WARD ROUND  the plan is labelled "queued", which is honest about the queue
 *               but not about what happens to it.
 *
 * What happens to it is this file's first test. The queue is deleted, without
 * warning and without a trace, at sign-out — and on the shared ward tablet this
 * is built for, sign-out is not a rare deliberate act: a PIN session drops
 * itself after TWO MINUTES of inactivity (App.SHARED_DEVICE_IDLE_MS). A nurse
 * who sends "bed 4 deteriorating, please review" in a corridor blackspot, then
 * puts the tablet on the trolley and walks to the bay, has a session that ends
 * itself before the wi-fi comes back. The message she watched appear in the
 * thread is destroyed, nobody is told, and she believes the doctor was called.
 *
 * A queue whose expected lifetime is shorter than the outage it exists to
 * survive is not a safety net. So these two writes are refused like the other
 * three, and the tests below hold that line at the call sites: offline, the
 * mutation is never even reached, so there is nothing to queue and nothing to
 * lose.
 */
import { render, screen, waitFor, act, fireEvent, cleanup } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

// ─────────────────────────────────────────────────────────────────────────────
// Shared fixtures
// ─────────────────────────────────────────────────────────────────────────────

const QUEUE_KEY = 'orb_offline_queue'

function setOnline(value: boolean) {
  Object.defineProperty(navigator, 'onLine', { value, configurable: true })
}

function readQueue(): Array<Record<string, unknown>> {
  return JSON.parse(localStorage.getItem(QUEUE_KEY) ?? '[]')
}

// ─────────────────────────────────────────────────────────────────────────────
// 1. WHY. What a queued write is actually worth on a shared tablet.
// ─────────────────────────────────────────────────────────────────────────────

describe('the lifetime of a write that reached the offline queue', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.resetModules()
  })
  afterEach(() => setOnline(true))

  it('is destroyed by the sign-out that a shared tablet performs on its own', async () => {
    // The REAL client, not the mock the Relay block below installs — this test
    // is about what api/client actually does to a queued write.
    const { sendMessage, clearOfflineQueue } =
      await vi.importActual<typeof import('../../api/client')>('../../api/client')
    localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR-A', name: 'Priya', role: 'nurse' }))
    setOnline(false)

    // The nurse sends an urgent message from a corridor blackspot. It resolves
    // — this is the optimistic copy — and she sees it land in the thread.
    const optimistic = await sendMessage('ch-bed4', 'Bed 4 deteriorating, please review')
    expect(optimistic.id).toBeLessThan(0) // negative id = pending, per client
    expect(readQueue()).toHaveLength(1)

    // She sets the tablet down. Two minutes later the PIN session drops itself
    // and App.logout runs, which calls exactly this.
    const discarded = clearOfflineQueue()

    // The message is gone. Note what the return value is used for at the call
    // site: nothing. Nobody is told, least of all the nurse who is no longer
    // signed in to be told.
    expect(discarded).toBe(1)
    expect(readQueue()).toHaveLength(0)
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// 2. RELAY. The message must be refused, not queued.
// ─────────────────────────────────────────────────────────────────────────────

const CHANNELS = [
  { id: 'ch-bed4', name: 'Bed 4 — Anita Rao', type: 'case_room', unread_count: 0, patient_id: 'P004' },
]

const sendMessageMock = vi.fn(async () => ({
  id: 1, channel_id: 'ch-bed4', sender_id: 'me', sender_name: 'Nurse Priya',
  sender_role: 'nurse', content: 'x', message_type: 'text',
  timestamp: '2026-08-07T10:00:00Z',
}))

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  const socketHandlers: Record<string, Function[]> = {}
  return {
    ...actual,
    getChannels: vi.fn(async () => CHANNELS),
    getAlerts: vi.fn(async () => []),
    getMessages: vi.fn(async () => []),
    sendMessage: (...args: unknown[]) => sendMessageMock(...(args as [])),
    resolveAlert: vi.fn(async () => ({})),
    surgicalTeamResponse: vi.fn(async () => ({})),
    markChannelRead: vi.fn(async () => ({ status: 'ok', marked_read: 0 })),
    getRoomFitFlags: vi.fn(async () => []),
    resolveRoomFit: vi.fn(async () => ({})),
    getUser: () => ({ id: 'me', name: 'Nurse Priya', role: 'nurse' }),
    socket: {
      on: (ev: string, fn: Function) => { (socketHandlers[ev] ||= []).push(fn) },
      off: (ev: string, fn: Function) => {
        socketHandlers[ev] = (socketHandlers[ev] || []).filter(f => f !== fn)
      },
      emit: vi.fn(),
    },
  }
})

import RelayPanel from './RelayPanel'

describe('Relay, with no connection', () => {
  beforeEach(() => {
    localStorage.clear()
    sendMessageMock.mockClear()
  })
  afterEach(() => { cleanup(); setOnline(true) })

  it('refuses the message instead of showing it as sent', async () => {
    setOnline(false)
    render(<RelayPanel />)

    fireEvent.click(await screen.findByText('Bed 4 — Anita Rao'))

    const input = await screen.findByPlaceholderText('Type a message…')

    await act(async () => {
      fireEvent.change(input, { target: { value: 'Bed 4 deteriorating, please review' } })
      await Promise.resolve()
    })
    await act(async () => {
      fireEvent.click(document.querySelector('.relay-chat__send') as HTMLElement)
      await Promise.resolve()
    })

    // THE POINT: the mutation is never reached, so nothing can be queued.
    expect(sendMessageMock).not.toHaveBeenCalled()
    expect(readQueue()).toHaveLength(0)

    // And she is TOLD. Silently dropping the optimistic copy and putting her
    // text back in the box is not a refusal — it looks like a tap that missed.
    // The words must rule out a later send.
    const refusal = await waitFor(() => {
      const el = document.querySelector('.relay-chat__send-error')
      expect(el).toBeTruthy()
      return el!
    })
    expect(refusal.getAttribute('role')).toBe('alert')
    expect(refusal.textContent).toMatch(/offline/i)
    expect(refusal.textContent).toMatch(/has not been saved/i)

    // The optimistic copy must not be left sitting in the thread looking sent.
    const thread = document.querySelector('.relay-chat__messages')
    expect(thread?.textContent ?? '').not.toContain('Bed 4 deteriorating')
  })
})
