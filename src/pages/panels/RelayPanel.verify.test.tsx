/**
 * INDEPENDENT VERIFICATION of the claimed cross-patient thread bleed in Relay,
 * plus the happy-path regressions the ownership guard could plausibly break.
 *
 * Attack model: an ordinary logged-in ward nurse on a slow ward link. No
 * privilege escalation — she is entitled to both rooms. The harm is that the
 * UI attributes one patient's drug traffic to the other patient's name.
 *
 * A  attack:   late response from an abandoned room must not paint under the
 *              open room's header, and must not clear the abandoned room's
 *              unread badge / fire its read receipt.
 * B  happy:    an ordinary single-channel open still renders, still marks read,
 *              still clears the badge.
 * C  happy:    back out mid-flight and RE-OPEN THE SAME room — the second load
 *              must still be applied (an over-tight guard would blank it).
 * D  happy:    a failed load shows the error, and Retry still works.
 * E  happy:    ordinary sequential channel switching, responses in order.
 */
import { StrictMode } from 'react'
import { render, screen, waitFor, act, fireEvent, cleanup } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void }
function defer<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const CHANNELS = [
  { id: 'ch-bed3', name: 'Bed 3 — Anita Rao', type: 'case_room', unread_count: 4, patient_id: 'P003' },
  { id: 'ch-bed7', name: 'Bed 7 — Rahul Menon', type: 'case_room', unread_count: 2, patient_id: 'P007' },
]

const msg = (id: number, channel_id: string, content: string) => ({
  id, channel_id, sender_id: 'u9', sender_name: 'Dr Iyer', sender_role: 'doctor',
  content, message_type: 'text', timestamp: '2026-08-06T10:00:00Z',
})

// Every getMessages call gets its OWN deferred, queued per channel, so a test
// can resolve the first and second load of the same channel independently.
type ThreadRow = ReturnType<typeof msg>
const queues: Record<string, Deferred<ThreadRow[]>[]> = {}
const getMessages = vi.fn((channelId: string) => {
  const d = defer<ThreadRow[]>()
  ;(queues[channelId] ||= []).push(d)
  return d.promise
})
const markChannelRead = vi.fn(async (_channelId: string) => ({ status: 'ok', marked_read: 0 }))
const sendMessage = vi.fn(async (channel_id: string, content: string) => ({
  id: 999, channel_id, sender_id: 'me', sender_name: 'Nurse Priya', sender_role: 'nurse',
  content, message_type: 'text', timestamp: '2026-08-06T10:05:00Z',
}))

// What a socket.io listener receives: one payload, or none.
type SocketHandler = (payload?: unknown) => void
const socketHandlers: Record<string, SocketHandler[]> = {}
vi.mock('../../api/client', () => ({
  getChannels: vi.fn(async () => CHANNELS),
  getAlerts: vi.fn(async () => []),
  getMessages: (id: string) => getMessages(id),
  sendMessage: (ch: string, content: string) => sendMessage(ch, content),
  resolveAlert: vi.fn(async () => ({})),
  surgicalTeamResponse: vi.fn(async () => ({})),
  markChannelRead: (id: string) => markChannelRead(id),
  getRoomFitFlags: vi.fn(async () => []),
  resolveRoomFit: vi.fn(async () => ({})),
  getSageMemory: vi.fn(async () => ({})),
  getSageActions: vi.fn(async () => []),
  transitionSageAction: vi.fn(async () => ({})),
  getUser: () => ({ id: 'me', name: 'Nurse Priya', role: 'nurse' }),
  socket: {
    on: (ev: string, fn: SocketHandler) => { (socketHandlers[ev] ||= []).push(fn) },
    off: (ev: string, fn: SocketHandler) => {
      socketHandlers[ev] = (socketHandlers[ev] || []).filter(f => f !== fn)
    },
    emit: vi.fn(),
  },
}))

import RelayPanel from './RelayPanel'

const settle = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }
const openChannel = async (name: string) => {
  const el = await screen.findByText(name)
  fireEvent.click(el)
}
const goBack = () => {
  const back = document.querySelector('.relay-chat__back') as HTMLElement
  expect(back).toBeTruthy()
  fireEvent.click(back)
}
const unreadBadgeFor = (name: string): string | null => {
  const row = screen.getByText(name).closest('.relay-channel-item') as HTMLElement | null
  if (!row) return null
  const badge = row.querySelector('.relay-channel-item__badge')
  return badge ? badge.textContent : null
}

beforeEach(() => {
  getMessages.mockClear()
  markChannelRead.mockClear()
  sendMessage.mockClear()
  for (const k of Object.keys(queues)) delete queues[k]
})
afterEach(() => cleanup())

describe('A — ATTACK: late thread from an abandoned room', () => {
  it('must not paint bed 3 content under the bed 7 header', async () => {
    render(<RelayPanel />)
    await openChannel('Bed 3 — Anita Rao')
    await waitFor(() => expect(queues['ch-bed3']?.length).toBe(1))

    goBack()
    await openChannel('Bed 7 — Rahul Menon')
    await waitFor(() => expect(queues['ch-bed7']?.length).toBe(1))

    // Bed 7 answers first — this is the room the nurse is looking at.
    await act(async () => { queues['ch-bed7'][0].resolve([msg(700, 'ch-bed7', 'BED SEVEN ceftriaxone 1g given')]) })
    expect(await screen.findByText(/BED SEVEN/)).toBeInTheDocument()
    markChannelRead.mockClear()

    // Bed 3's abandoned fetch finally lands.
    await act(async () => { queues['ch-bed3'][0].resolve([msg(300, 'ch-bed3', 'BED THREE potassium 40mmol running')]) })
    await settle()

    // Evidence dump: what the header says vs what is under it.
    console.log('[EVIDENCE] header =', document.querySelector('.relay-chat__title')?.textContent)
    console.log('[EVIDENCE] thread =', [...document.querySelectorAll('.relay-msg__content')].map(n => n.textContent))

    expect(screen.getByText('Bed 7 — Rahul Menon')).toBeInTheDocument()
    expect(screen.queryByText(/BED THREE/)).toBeNull()
    expect(screen.getByText(/BED SEVEN/)).toBeInTheDocument()
    // ...and the abandoned room's read receipt must NOT fire.
    expect(markChannelRead).not.toHaveBeenCalledWith('ch-bed3')
  })
})

describe('B — HAPPY PATH: an ordinary channel open', () => {
  it('renders the thread, fires the read receipt, clears the badge', async () => {
    render(<RelayPanel />)
    expect(await screen.findByText('Bed 3 — Anita Rao')).toBeInTheDocument()
    expect(unreadBadgeFor('Bed 3 — Anita Rao')).toBe('4')

    await openChannel('Bed 3 — Anita Rao')
    await waitFor(() => expect(queues['ch-bed3']?.length).toBe(1))
    await act(async () => { queues['ch-bed3'][0].resolve([msg(300, 'ch-bed3', 'BED THREE potassium 40mmol running')]) })

    expect(await screen.findByText(/BED THREE/)).toBeInTheDocument()
    expect(screen.getByText('Bed 3 — Anita Rao')).toBeInTheDocument()
    expect(document.querySelector('.orb-panel-loading')).toBeNull()
    expect(markChannelRead).toHaveBeenCalledWith('ch-bed3')

    goBack()
    await waitFor(() => expect(unreadBadgeFor('Bed 3 — Anita Rao')).toBeNull())
  })
})

describe('C — HAPPY PATH: back out mid-flight, re-open the SAME room', () => {
  it('still shows the re-opened room and prefers the fresh load', async () => {
    render(<RelayPanel />)
    await openChannel('Bed 3 — Anita Rao')
    await waitFor(() => expect(queues['ch-bed3']?.length).toBe(1))

    goBack()
    await openChannel('Bed 3 — Anita Rao')
    await waitFor(() => expect(queues['ch-bed3']?.length).toBe(2))

    // The stale first load lands first, carrying an OLD copy of the thread.
    await act(async () => { queues['ch-bed3'][0].resolve([msg(299, 'ch-bed3', 'STALE first load')]) })
    // Then the load that belongs to the room she is actually in.
    await act(async () => { queues['ch-bed3'][1].resolve([msg(300, 'ch-bed3', 'FRESH second load')]) })
    await settle()

    expect(await screen.findByText(/FRESH second load/)).toBeInTheDocument()
    expect(screen.queryByText(/STALE first load/)).toBeNull()
    // The spinner must be gone — an over-tight guard would strand it on.
    expect(document.querySelector('.orb-panel-loading')).toBeNull()
    expect(markChannelRead).toHaveBeenCalledWith('ch-bed3')
  })
})

describe('D — HAPPY PATH: failed load, then Retry', () => {
  it('shows the error and recovers on retry', async () => {
    render(<RelayPanel />)
    await openChannel('Bed 7 — Rahul Menon')
    await waitFor(() => expect(queues['ch-bed7']?.length).toBe(1))

    await act(async () => { queues['ch-bed7'][0].reject(new Error('Network request failed')) })
    await settle()
    expect(await screen.findByText(/did not load/i)).toBeInTheDocument()

    const retry = document.querySelector('.relay-thread-error__retry') as HTMLElement
    expect(retry).toBeTruthy()
    fireEvent.click(retry)
    await waitFor(() => expect(queues['ch-bed7']?.length).toBe(2))
    await act(async () => { queues['ch-bed7'][1].resolve([msg(700, 'ch-bed7', 'BED SEVEN ceftriaxone 1g given')]) })

    expect(await screen.findByText(/BED SEVEN/)).toBeInTheDocument()
    expect(screen.queryByText(/did not load/i)).toBeNull()
    expect(document.querySelector('.orb-panel-loading')).toBeNull()
  })
})

describe('F — ATTACK 2: message:new delivered on the PERSONAL socket room', () => {
  /* server/ws/realtime.py:173 puts every socket permanently in `user:<id>`.
     server/routes/surgery_routes.py:400 and
     server/services/sage_handoff_service.py:489 emit `message:new` into that
     personal room carrying the OTHER patient's channel_id. RelayPanel's
     handleNewMsg (RelayPanel.tsx:108) appends without checking channel_id —
     handleTyping right below it DOES check. No race required. */
  // LIVE DEFECT — `it.fails` asserts the bleed is STILL present. It goes red
  // the moment handleNewMsg starts checking msg.channel_id, which is the fix.
  it.fails('must not append a foreign-channel message to the open thread', async () => {
    render(<RelayPanel />)
    await openChannel('Bed 7 — Rahul Menon')
    await waitFor(() => expect(queues['ch-bed7']?.length).toBe(1))
    await act(async () => { queues['ch-bed7'][0].resolve([msg(700, 'ch-bed7', 'BED SEVEN ceftriaxone 1g given')]) })
    expect(await screen.findByText(/BED SEVEN/)).toBeInTheDocument()

    const handlers = socketHandlers['message:new'] || []
    expect(handlers.length).toBeGreaterThan(0)
    await act(async () => {
      handlers[handlers.length - 1](
        msg(301, 'ch-bed3', 'BED THREE transfuse 2 units cross-matched')
      )
    })

    console.log('[EVIDENCE F] header =', document.querySelector('.relay-chat__title')?.textContent)
    console.log('[EVIDENCE F] thread =', [...document.querySelectorAll('.relay-msg__content')].map(n => n.textContent))
    expect(screen.getByText('Bed 7 — Rahul Menon')).toBeInTheDocument()
    expect(screen.queryByText(/BED THREE/)).toBeNull()
  })
})

describe('G — ATTACK 3: post-send refresh landing after a channel switch', () => {
  /* handleSend re-fetches the thread AFTER the send round-trip
     (RelayPanel.tsx:300-302) and calls setMessages unconditionally — the
     ownership stamp added to loadMessages does not cover this path. */
  // LIVE DEFECT — `it.fails` asserts the bleed is STILL present. It goes red
  // once handleSend's refresh carries the same ownership stamp loadMessages got.
  it.fails('must not repaint the open room with the room the message was sent to', async () => {
    render(<RelayPanel />)
    await openChannel('Bed 3 — Anita Rao')
    await waitFor(() => expect(queues['ch-bed3']?.length).toBe(1))
    await act(async () => { queues['ch-bed3'][0].resolve([msg(300, 'ch-bed3', 'BED THREE potassium 40mmol running')]) })
    expect(await screen.findByText(/BED THREE/)).toBeInTheDocument()

    // Nurse types into bed 3 and hits send.
    const input = document.querySelector('.relay-chat__text-input') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'K+ 5.1, holding the infusion' } })
    fireEvent.click(document.querySelector('.relay-chat__send') as HTMLElement)
    // Send completes; the post-send refresh for bed 3 is now in flight.
    await waitFor(() => expect(queues['ch-bed3']?.length).toBe(2))

    // She does not wait for it — back out, open bed 7.
    goBack()
    await openChannel('Bed 7 — Rahul Menon')
    await waitFor(() => expect(queues['ch-bed7']?.length).toBe(1))
    await act(async () => { queues['ch-bed7'][0].resolve([msg(700, 'ch-bed7', 'BED SEVEN ceftriaxone 1g given')]) })
    expect(await screen.findByText(/BED SEVEN/)).toBeInTheDocument()

    // Bed 3's post-send refresh finally lands.
    await act(async () => {
      queues['ch-bed3'][1].resolve([
        msg(300, 'ch-bed3', 'BED THREE potassium 40mmol running'),
        msg(302, 'ch-bed3', 'BED THREE K+ 5.1, holding the infusion'),
      ])
    })
    await settle()

    console.log('[EVIDENCE G] header =', document.querySelector('.relay-chat__title')?.textContent)
    console.log('[EVIDENCE G] thread =', [...document.querySelectorAll('.relay-msg__content')].map(n => n.textContent))
    expect(screen.getByText('Bed 7 — Rahul Menon')).toBeInTheDocument()
    expect(screen.queryByText(/BED THREE/)).toBeNull()
    expect(screen.getByText(/BED SEVEN/)).toBeInTheDocument()
  })
})

describe('E — HAPPY PATH: ordinary sequential switching', () => {
  it('each room shows its own thread when responses arrive in order', async () => {
    render(<RelayPanel />)
    await openChannel('Bed 3 — Anita Rao')
    await waitFor(() => expect(queues['ch-bed3']?.length).toBe(1))
    await act(async () => { queues['ch-bed3'][0].resolve([msg(300, 'ch-bed3', 'BED THREE potassium 40mmol running')]) })
    expect(await screen.findByText(/BED THREE/)).toBeInTheDocument()

    goBack()
    await openChannel('Bed 7 — Rahul Menon')
    await waitFor(() => expect(queues['ch-bed7']?.length).toBe(1))
    await act(async () => { queues['ch-bed7'][0].resolve([msg(700, 'ch-bed7', 'BED SEVEN ceftriaxone 1g given')]) })
    expect(await screen.findByText(/BED SEVEN/)).toBeInTheDocument()
    expect(screen.queryByText(/BED THREE/)).toBeNull()

    goBack()
    await openChannel('Bed 3 — Anita Rao')
    await waitFor(() => expect(queues['ch-bed3']?.length).toBe(2))
    await act(async () => { queues['ch-bed3'][1].resolve([msg(301, 'ch-bed3', 'BED THREE noradrenaline weaning')]) })
    expect(await screen.findByText(/noradrenaline/)).toBeInTheDocument()
    expect(screen.queryByText(/BED SEVEN/)).toBeNull()
  })
})

describe('H — HAPPY PATH: under StrictMode (src/main.tsx:105 wraps the real app)', () => {
  /* Observed: the channel-open effect issues ONE load even under StrictMode
     (the double-invoked mount effect runs while selectedChannel is still null).
     This asserts the guard does not strand the spinner or blank the thread
     under the wrapper the real app actually uses. */
  it('still loads and displays the thread', async () => {
    render(<StrictMode><RelayPanel /></StrictMode>)
    await openChannel('Bed 3 — Anita Rao')
    await waitFor(() => expect((queues['ch-bed3']?.length ?? 0)).toBeGreaterThanOrEqual(1))
    // Resolve EVERY in-flight load for this channel, oldest first.
    await act(async () => { for (const d of queues['ch-bed3']) d.resolve([msg(300, 'ch-bed3', 'BED THREE potassium 40mmol running')]) })
    await settle()

    console.log('[EVIDENCE H] loads issued =', queues['ch-bed3'].length,
                '| thread =', [...document.querySelectorAll('.relay-msg__content')].map(n => n.textContent))
    expect(await screen.findByText(/BED THREE/)).toBeInTheDocument()
    expect(document.querySelector('.orb-panel-loading')).toBeNull()
    expect(markChannelRead).toHaveBeenCalledWith('ch-bed3')
  })
})
