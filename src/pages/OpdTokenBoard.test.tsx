/**
 * The waiting-room token board screen (opd-9).
 *
 * What it must never get wrong:
 *   1. THE KEY. Read from the address's fragment, never the query string, and
 *      sent only in the X-Orb-Board-Key header, with no staff credentials.
 *   2. WHAT IT SHOWS. Per doctor: the name, the room, the token being called
 *      and the next tokens. Nothing else the server could send is drawn.
 *   3. HONESTY. A refused key clears the screen and stops asking; a failed
 *      read keeps the last tokens with the time they were read and says they
 *      are not current; switched off says so and asks nothing but /api/health.
 *   4. POLLING. It reads again every 10 seconds.
 */
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import '../i18n'
import { resetFeatureFlags, resetModuleFlags } from '../shellConfig'
import { boardKeyFromHash, boardLink } from '../api/opdBoardScreen'
import OpdTokenBoard from './OpdTokenBoard'

const SNAPSHOT = {
  board_label: 'Medicine waiting hall',
  department: 'General Medicine',
  visit_date: '2026-10-09',
  generated_at: '2026-10-09T05:00:00Z',
  poll_seconds: 10,
  doctors: [
    { doctor_name: 'Dr Meera', room: '4', calling: 12, next: [13, 14, 15] },
    { doctor_name: 'Dr Arun', room: null, calling: null, next: [] },
  ],
}

type Reply = () => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>
let boardReplies: Reply[] = []
let opdOn = true

function ok(body: unknown): Reply {
  return async () => ({ ok: true, status: 200, json: async () => body })
}

function status(code: number): Reply {
  return async () => ({ ok: false, status: code, json: async () => ({ detail: {} }) })
}

const fetchSpy = vi.fn(async (url: string) => {
  if (String(url).endsWith('/api/health')) {
    return { ok: true, status: 200, json: async () => ({ modules: {}, features: { opd: opdOn } }) }
  }
  if (String(url).endsWith('/api/frontdesk/opd/board')) {
    const next = boardReplies.length > 1 ? boardReplies.shift()! : boardReplies[0]
    if (!next) throw new Error('no board reply')
    return next()
  }
  throw new Error(`unexpected fetch: ${url}`)
})

function boardCalls() {
  return (fetchSpy.mock.calls as unknown as Array<[string, RequestInit | undefined]>)
    .filter(([url]) => String(url).endsWith('/api/frontdesk/opd/board'))
}

beforeEach(() => {
  resetModuleFlags()
  resetFeatureFlags()
  opdOn = true
  boardReplies = [ok(SNAPSHOT)]
  fetchSpy.mockClear()
  vi.stubGlobal('fetch', fetchSpy)
  window.history.pushState({}, '', '/display/opd#key=wall-key-1')
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  window.history.pushState({}, '', '/')
})

describe('the board key', () => {
  it('comes from the fragment and goes out in the header only, with no credentials', async () => {
    render(<OpdTokenBoard />)
    expect(await screen.findByText('Medicine waiting hall')).toBeInTheDocument()
    const [url, init] = boardCalls()[0]
    expect(url).toBe('/api/frontdesk/opd/board')
    expect((init?.headers as Record<string, string>)['X-Orb-Board-Key']).toBe('wall-key-1')
    expect(init?.credentials).toBe('omit')
    expect(init?.body).toBeUndefined()
    for (const [u] of fetchSpy.mock.calls as unknown as Array<[string]>) {
      expect(String(u)).not.toContain('wall-key-1')
    }
  })

  it('never reads a key from the query string', async () => {
    window.history.pushState({}, '', '/display/opd?key=wall-key-1')
    render(<OpdTokenBoard />)
    expect(await screen.findByText(/This screen has no board key/)).toBeInTheDocument()
    expect(boardCalls()).toHaveLength(0)
  })

  it('parses and builds the fragment form only', () => {
    expect(boardKeyFromHash('#key=abc%2Bdef')).toBe('abc+def')
    expect(boardKeyFromHash('')).toBeNull()
    expect(boardKeyFromHash('#other=1')).toBeNull()
    expect(boardLink('http://orb.local/', 'a+b')).toBe('http://orb.local/display/opd#key=a%2Bb')
  })
})

describe('what the board shows', () => {
  it('shows each doctor, room, the token being called and the next ones', async () => {
    const { container } = render(<OpdTokenBoard />)
    expect(await screen.findByText('Dr Meera')).toBeInTheDocument()
    expect(screen.getByText('Room 4')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    for (const t of ['13', '14', '15']) expect(screen.getByText(t)).toBeInTheDocument()
    expect(screen.getByText('Dr Arun')).toBeInTheDocument()
    expect(screen.getByText('No one waiting')).toBeInTheDocument()
    const text = container.textContent ?? ''
    for (const word of ['UHID', 'patient', 'Patient', 'score', 'NEWS2']) expect(text).not.toContain(word)
  })

  it('draws nothing it was not meant to, whatever the server adds', async () => {
    boardReplies = [ok({
      ...SNAPSHOT,
      patient_name: 'Should Not Show',
      doctors: [{ ...SNAPSHOT.doctors[0], patient_name: 'Hidden Person', uhid: 'UH-9' }],
    })]
    const { container } = render(<OpdTokenBoard />)
    await screen.findByText('Dr Meera')
    expect(container.textContent).not.toContain('Should Not Show')
    expect(container.textContent).not.toContain('Hidden Person')
    expect(container.textContent).not.toContain('UH-9')
  })
})

describe('honesty', () => {
  it('clears the screen and stops asking when the key is refused', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    boardReplies = [status(401)]
    render(<OpdTokenBoard />)
    expect(await screen.findByText(/board key is not valid/)).toBeInTheDocument()
    const asked = boardCalls().length
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(boardCalls().length).toBe(asked)
  })

  it('keeps the last tokens with their time and says they are not current', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    boardReplies = [ok(SNAPSHOT), status(500), ok(SNAPSHOT)]
    render(<OpdTokenBoard />)
    expect(await screen.findByText('Dr Meera')).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    await waitFor(() => expect(screen.getByText(/Not current: last updated/)).toBeInTheDocument())
    expect(screen.getByText('Dr Meera')).toBeInTheDocument()
  })

  it('says it is switched off and asks nothing but the health check', async () => {
    opdOn = false
    render(<OpdTokenBoard />)
    await waitFor(() => expect(screen.getByRole('status'))
      .toHaveTextContent('OPD token board is switched off on this site.'))
    expect(boardCalls()).toHaveLength(0)
  })

  it('says it is switched off when the board answers 404', async () => {
    boardReplies = [status(404)]
    render(<OpdTokenBoard />)
    await waitFor(() => expect(screen.getByRole('status'))
      .toHaveTextContent('OPD token board is switched off on this site.'))
  })
})

describe('polling', () => {
  it('reads again every 10 seconds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<OpdTokenBoard />)
    await screen.findByText('Dr Meera')
    const first = boardCalls().length
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(boardCalls().length).toBe(first + 1)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(boardCalls().length).toBe(first + 2)
  })
})
