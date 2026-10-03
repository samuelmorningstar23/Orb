/**
 * Relay unread badges: a message the reader watched arrive is read (F087).
 *
 * The open room was marked read only when it opened. Every later message
 * appended from the socket stayed unread on the server, so backing out to the
 * list brought the badge straight back for messages the nurse had just read.
 * A room still loading, or one open behind another tab, has shown nothing, and
 * must not be marked read.
 */
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const CHANNELS = [
  { id: 'ch-bed3', name: 'Bed 3 Anita Rao', type: 'case_room', unread_count: 2, patient_id: 'P003' },
]

const msg = (id: number, sender_id = 'u9') => ({
  id, channel_id: 'ch-bed3', sender_id, sender_name: 'Dr Iyer', sender_role: 'doctor',
  content: `Obs set ${id}`, message_type: 'text', timestamp: '2026-10-03T05:00:00Z',
})

let holdThread = false
let releaseThread: (() => void) | null = null
const getMessages = vi.fn(() => {
  if (!holdThread) return Promise.resolve([msg(1)])
  return new Promise<ReturnType<typeof msg>[]>(res => { releaseThread = () => res([msg(1)]) })
})
const getChannels = vi.fn(async () => CHANNELS)
const markChannelRead = vi.fn<(id: string) => Promise<{ status: string; marked_read: number }>>(async () => ({ status: 'ok', marked_read: 1 }))

type SocketHandler = (payload: unknown) => void
const socketHandlers: Record<string, SocketHandler[]> = {}
vi.mock('../../api/client', () => ({
  getChannels: () => getChannels(),
  getAlerts: vi.fn(async () => []),
  getMessages: () => getMessages(),
  sendMessage: vi.fn(async () => ({})),
  resolveAlert: vi.fn(async () => ({})),
  surgicalTeamResponse: vi.fn(async () => ({})),
  markChannelRead: (id: string) => markChannelRead(id),
  getRoomFitFlags: vi.fn(async () => []),
  resolveRoomFit: vi.fn(async () => ({})),
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

function Shell({ hidden }: { hidden: boolean }) {
  // The clinician shell keeps the panel mounted behind other tabs this way.
  return <div style={{ display: hidden ? 'none' : 'contents' }}><RelayPanel /></div>
}

async function openRoom() {
  fireEvent.click(await screen.findByText('Bed 3 Anita Rao'))
  await screen.findByText('Obs set 1')
  await waitFor(() => expect(markChannelRead).toHaveBeenCalledTimes(1))
}

function arrive(m: ReturnType<typeof msg>) {
  act(() => { (socketHandlers['message:new'] || []).forEach(fn => fn(m)) })
}

describe('Relay read receipts for live messages', () => {
  beforeEach(() => {
    holdThread = false
    releaseThread = null
    markChannelRead.mockClear()
    getChannels.mockClear()
  })
  afterEach(() => { vi.useRealTimers() })

  it('marks a message read when it lands in the open, visible room', async () => {
    render(<Shell hidden={false} />)
    await openRoom()

    vi.useFakeTimers()
    arrive(msg(2))
    arrive(msg(3))
    act(() => { vi.advanceTimersByTime(1600) })
    // Two arrivals, one receipt.
    expect(markChannelRead).toHaveBeenCalledTimes(2)
    expect(markChannelRead).toHaveBeenLastCalledWith('ch-bed3')
  })

  it('sends no receipt for the reader\'s own message', async () => {
    render(<Shell hidden={false} />)
    await openRoom()

    vi.useFakeTimers()
    arrive(msg(4, 'me'))
    act(() => { vi.advanceTimersByTime(1600) })
    expect(markChannelRead).toHaveBeenCalledTimes(1)
  })

  it('leaves a message unread while the panel is behind another tab', async () => {
    const { rerender } = render(<Shell hidden={false} />)
    await openRoom()

    rerender(<Shell hidden />)
    vi.useFakeTimers()
    arrive(msg(5))
    act(() => { vi.advanceTimersByTime(1600) })
    expect(markChannelRead).toHaveBeenCalledTimes(1)

    // Back in front: the next arrival finds the room on screen.
    rerender(<Shell hidden={false} />)
    arrive(msg(6))
    act(() => { vi.advanceTimersByTime(1600) })
    expect(markChannelRead).toHaveBeenCalledTimes(2)
  })

  it('persists the receipt before the list reloads its counts on back', async () => {
    render(<Shell hidden={false} />)
    await openRoom()
    arrive(msg(7))
    getChannels.mockClear()

    fireEvent.click(document.querySelector('.relay-chat__back') as HTMLElement)
    await waitFor(() => expect(getChannels).toHaveBeenCalled())
    const lastMark = markChannelRead.mock.invocationCallOrder.at(-1)!
    expect(markChannelRead).toHaveBeenCalledTimes(2)
    expect(lastMark).toBeLessThan(getChannels.mock.invocationCallOrder[0])
  })

  it('never marks read a room the reader backed out of while it was loading', async () => {
    holdThread = true
    render(<Shell hidden={false} />)
    fireEvent.click(await screen.findByText('Bed 3 Anita Rao'))
    await waitFor(() => expect(releaseThread).toBeTruthy())

    fireEvent.click(document.querySelector('.relay-chat__back') as HTMLElement)
    await screen.findByText('Bed 3 Anita Rao')
    await act(async () => { releaseThread!(); await Promise.resolve() })

    expect(markChannelRead).not.toHaveBeenCalled()
  })
})
