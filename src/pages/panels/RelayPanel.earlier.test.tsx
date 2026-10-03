/**
 * A case room shows its newest page, and "Show earlier messages" reads the
 * page before it. A thread used to show its newest 50 and nothing earlier, so
 * the start of a critical patient's discussion was out of reach. Also: a
 * simulator's post carries a "Simulated" badge.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { RELAY_PAGE_SIZE } from '../../lib/relayPaging'

const CHANNELS = [
  { id: 'ch-bed3', name: 'Bed 3, Anita Rao', type: 'case_room', unread_count: 0, patient_id: 'P003' },
]

const msg = (id: number, content: string, extra: Record<string, unknown> = {}) => ({
  id, channel_id: 'ch-bed3', sender_id: 'u9', sender_name: 'Dr Iyer', sender_role: 'doctor',
  content, message_type: 'text',
  timestamp: new Date(Date.UTC(2026, 9, 1, 0, 0, id)).toISOString(),
  ...extra,
})

// Messages 101..150 are the newest page; 61..100 are older (a short page).
const NEWEST = Array.from({ length: RELAY_PAGE_SIZE }, (_, i) => msg(101 + i, `newer note ${101 + i}`))
const OLDER = Array.from({ length: 40 }, (_, i) => msg(61 + i, `older note ${61 + i}`))

const getMessages = vi.fn(async (_id: string, beforeId?: number) => (beforeId == null ? NEWEST : OLDER))

vi.mock('../../api/client', () => ({
  getChannels: vi.fn(async () => CHANNELS),
  getAlerts: vi.fn(async () => []),
  getMessages: (id: string, beforeId?: number) => getMessages(id, beforeId),
  sendMessage: vi.fn(async () => ({})),
  resolveAlert: vi.fn(async () => ({})),
  surgicalTeamResponse: vi.fn(async () => ({})),
  markChannelRead: vi.fn(async () => ({ status: 'ok', marked_read: 0 })),
  getRoomFitFlags: vi.fn(async () => []),
  resolveRoomFit: vi.fn(async () => ({})),
  getUser: () => ({ id: 'me', name: 'Nurse Priya', role: 'nurse' }),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

import RelayPanel from './RelayPanel'

async function openRoom() {
  render(<RelayPanel />)
  fireEvent.click(await screen.findByText('Bed 3, Anita Rao'))
  await screen.findByText('newer note 150')
}

describe('Relay earlier messages', () => {
  beforeEach(() => { getMessages.mockClear() })

  it('loads the page before the oldest shown message and keeps the newer ones', async () => {
    await openRoom()
    expect(screen.queryByText('older note 100')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Show earlier messages' }))

    expect(await screen.findByText('older note 100')).toBeInTheDocument()
    expect(getMessages).toHaveBeenLastCalledWith('ch-bed3', 101)
    expect(screen.getByText('older note 61')).toBeInTheDocument()
    expect(screen.getByText('newer note 150')).toBeInTheDocument()

    // Oldest first, older page above the newer one.
    const contents = [...document.querySelectorAll('.relay-msg__content')].map(n => n.textContent)
    expect(contents.indexOf('older note 100')).toBeLessThan(contents.indexOf('newer note 101'))

    // A short page means the start of the thread: the button goes.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Show earlier messages' })).toBeNull())
  })

  it('offers no earlier messages when the first page is already short', async () => {
    getMessages.mockImplementationOnce(async () => OLDER)
    render(<RelayPanel />)
    fireEvent.click(await screen.findByText('Bed 3, Anita Rao'))
    await screen.findByText('older note 100')
    expect(screen.queryByRole('button', { name: 'Show earlier messages' })).toBeNull()
  })

  it('badges a simulator post as simulated', async () => {
    getMessages.mockImplementationOnce(async () => [
      msg(5, 'LAB RESULT: BMP', { is_bot: true, source: 'simulated', sender_name: 'Orb Lab System' }),
      msg(6, 'Seen, plan unchanged'),
    ])
    render(<RelayPanel />)
    fireEvent.click(await screen.findByText('Bed 3, Anita Rao'))
    await screen.findByText('LAB RESULT: BMP')
    expect(screen.getAllByText('Simulated')).toHaveLength(1)
  })
})
