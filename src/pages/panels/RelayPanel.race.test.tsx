/**
 * ATTACK: cross-patient thread bleed in Relay.
 *
 * A ward nurse opens bed 3's case room. The thread is slow (a busy appliance,
 * a wifi dead spot on the corridor). She backs out and opens bed 7's room
 * instead. Bed 7 loads. THEN bed 3's response finally lands.
 *
 * `loadMessages` has no ownership check on the response it applies, so bed 3's
 * messages are written into state and rendered under bed 7's header.
 */
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void }
function defer<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const CHANNELS = [
  { id: 'ch-bed3', name: 'Bed 3 — Anita Rao', type: 'case_room', unread_count: 0, patient_id: 'P003' },
  { id: 'ch-bed7', name: 'Bed 7 — Rahul Menon', type: 'case_room', unread_count: 0, patient_id: 'P007' },
]

const msg = (id: number, channel_id: string, content: string) => ({
  id, channel_id, sender_id: 'u9', sender_name: 'Dr Iyer', sender_role: 'doctor',
  content, message_type: 'text', timestamp: '2026-08-06T10:00:00Z',
})

const pending: Record<string, Deferred<any[]>> = {}
const getMessages = vi.fn((channelId: string) => {
  pending[channelId] = defer<any[]>()
  return pending[channelId].promise
})

const socketHandlers: Record<string, Function[]> = {}
vi.mock('../../api/client', () => ({
  getChannels: vi.fn(async () => CHANNELS),
  getAlerts: vi.fn(async () => []),
  getMessages: (id: string) => getMessages(id),
  sendMessage: vi.fn(async () => ({})),
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
}))

import RelayPanel from './RelayPanel'

describe('Relay channel switch under a slow network', () => {
  beforeEach(() => { getMessages.mockClear() })

  it('never renders bed 3 traffic inside bed 7 room', async () => {
    render(<RelayPanel />)

    // Ward list loads.
    const bed3 = await screen.findByText('Bed 3 — Anita Rao')
    fireEvent.click(bed3)

    // Bed 3's thread is in flight, and slow.
    await waitFor(() => expect(pending['ch-bed3']).toBeTruthy())

    // Nurse gives up, goes back, opens bed 7 instead.
    const back = document.querySelector('.relay-chat__back') as HTMLElement
    expect(back).toBeTruthy()
    fireEvent.click(back)
    const bed7 = await screen.findByText('Bed 7 — Rahul Menon')
    fireEvent.click(bed7)
    await waitFor(() => expect(pending['ch-bed7']).toBeTruthy())

    // Bed 7 answers first.
    await act(async () => {
      pending['ch-bed7'].resolve([msg(700, 'ch-bed7', 'BED SEVEN ceftriaxone 1g given')])
      await Promise.resolve()
    })
    expect(await screen.findByText(/BED SEVEN/)).toBeInTheDocument()

    // ...and only now does bed 3 come back.
    await act(async () => {
      pending['ch-bed3'].resolve([msg(300, 'ch-bed3', 'BED THREE potassium 40mmol running')])
      await Promise.resolve()
    })

    // The header still says bed 7. What is under it must still be bed 7.
    expect(screen.getByText('Bed 7 — Rahul Menon')).toBeInTheDocument()
    expect(screen.queryByText(/BED THREE/)).toBeNull()
    expect(screen.getByText(/BED SEVEN/)).toBeInTheDocument()
  })
})
