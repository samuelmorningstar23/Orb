/**
 * Relay rendering and layout.
 *
 *  - The Vigil bot's disclaimer is _italic_ markdown and its alerts are
 *    several lines: both used to render as literal underscores in one run-on
 *    paragraph. snake_case in a pasted line must stay as typed.
 *  - The list read Case Rooms (with its sort) ABOVE Action Required.
 *  - On a phone the room grew to the whole thread, so it opened at the oldest
 *    message with the composer far below the fold, and the Sage drawer had no
 *    way to open below 900px.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const CHANNELS = [
  { id: 'ch-bed3', name: 'Bed 3 Anita Rao', type: 'case_room', unread_count: 0, patient_id: 'P003', has_alert: true },
  { id: 'dept-gm', name: 'General Medicine', type: 'department', unread_count: 0 },
]
const ALERT = {
  id: 41, channel_id: 'ch-bed3', sender_id: 'bot_vigil', sender_name: 'Orb Vigil',
  sender_role: 'intelligence', content: 'VIGIL ALERT: RR 28', message_type: 'alert',
  timestamp: '2026-10-02T08:00:00Z',
}

vi.mock('../../api/client', () => ({
  getChannels: vi.fn(async () => CHANNELS),
  getAlerts: vi.fn(async () => [ALERT]),
  getMessages: vi.fn(async () => []),
  sendMessage: vi.fn(async () => ({})),
  resolveAlert: vi.fn(async () => ({})),
  surgicalTeamResponse: vi.fn(async () => ({})),
  markChannelRead: vi.fn(async () => ({ status: 'ok', marked_read: 0 })),
  getRoomFitFlags: vi.fn(async () => []),
  resolveRoomFit: vi.fn(async () => ({})),
  getSageMemory: vi.fn(async () => ({})),
  getSageActions: vi.fn(async () => []),
  transitionSageAction: vi.fn(async () => ({})),
  getUser: () => ({ id: 'me', name: 'Nurse Priya', role: 'nurse' }),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

import RelayPanel from './RelayPanel'
import { formatMessage } from '../../lib/relayFormat'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('formatMessage', () => {
  it('renders the Vigil disclaimer in italics, not with literal underscores', () => {
    const html = formatMessage(
      "**Standing guidance**: Monitor closely.\n_This is Orb's fixed degraded-mode line, not an assessment of this patient._",
    )
    expect(html).toContain("<em>This is Orb's fixed degraded-mode line, not an assessment of this patient.</em>")
    expect(html).not.toMatch(/(^|\s)_This/)
    expect(html).toContain('<strong>Standing guidance</strong>')
  })

  it('leaves snake_case and dunder names exactly as typed', () => {
    for (const text of ['set risk_level_high on bed_3', 'see __init__ and _private_var', 'a_b_c']) {
      expect(formatMessage(text)).toBe(text)
    }
  })

  it('keeps the line breaks, which the message styles then show', () => {
    expect(formatMessage('line one\nline two')).toBe('line one\nline two')
  })
})

describe('case list order', () => {
  it('puts Action Required and the search above the Case Rooms heading', async () => {
    render(<RelayPanel />)
    const action = await screen.findByRole('heading', { name: /Action Required/ })
    const rooms = screen.getByRole('heading', { name: 'Case Rooms' })
    const search = screen.getByPlaceholderText(/Search patient/)
    const follows = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
    expect(follows(action, search)).toBe(true)
    expect(follows(search, rooms)).toBe(true)
    expect(screen.getByRole('heading', { name: 'Department' })).toBeInTheDocument()
  })

  it('gives every row the same dot slot and badge column', async () => {
    render(<RelayPanel />)
    const row = (await screen.findByText('General Medicine')).closest('.relay-channel-item') as HTMLElement
    expect(row.classList.contains('relay-channel-item--case')).toBe(true)
    expect(row.querySelector('.relay-case__dot')).not.toBeNull()
    expect(row.querySelector('.relay-case__badge-slot')).not.toBeNull()
  })
})

describe('case room on a phone', () => {
  beforeEach(() => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q === '(max-width: 768px)',
      media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    }))
    vi.stubGlobal('scrollTo', vi.fn())
    vi.stubGlobal('innerHeight', 812)
  })

  it('bounds the room to the viewport so the thread scrolls inside it', async () => {
    render(<RelayPanel />)
    fireEvent.click((await screen.findAllByText('Bed 3 Anita Rao'))[0])
    const room = document.querySelector('.relay-room-container') as HTMLElement
    await waitFor(() => expect(room.style.height).toBe('812px'))
    expect(window.scrollTo).toHaveBeenCalledWith(0, 0)
    // the old inline 400px floor is gone
    expect((document.querySelector('.relay-chat') as HTMLElement).style.minWidth).toBe('')
  })

  it('opens and closes the Sage drawer from the room', async () => {
    render(<RelayPanel />)
    fireEvent.click((await screen.findAllByText('Bed 3 Anita Rao'))[0])
    const toggle = await screen.findByRole('button', { name: 'Sage' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await act(async () => { fireEvent.click(toggle) })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByText('Sage has not summarised this room yet.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await act(async () => { fireEvent.click(toggle) })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })
})
