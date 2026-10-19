/**
 * The live bed board (adt-19) and the ward clerk's admission button (adt-17b).
 *
 * What it must never get wrong:
 *
 * 1. ONE REFETCH PER EVENT. A server 'adt:changed' re-reads the board once;
 *    a burst (a move between two wards is two events) is still one re-read.
 * 2. THE PAYLOAD CARRIES NO PATIENT FIELD. The client takes the ward from the
 *    event and nothing else, whatever else arrives.
 * 3. ANOTHER WARD'S EVENT IS IGNORED while one ward is on screen; an event
 *    naming no ward always re-reads.
 * 4. LOSING THE SOCKET FALLS BACK TO POLLING: the 30 second poll keeps
 *    running while the socket is down, and the board re-reads once when the
 *    socket comes back, for what it missed.
 * 5. A CHANGE MADE ON THIS SCREEN ('orb:data-changed') re-reads at once.
 * 6. ONLY THE WARD CLERK gets 'Admit a patient', which opens the admission
 *    form in its registration-only mode.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import i18n from '../../i18n'
import type { BedBoard } from '../../api/adt/board'
import { announceDataChanged } from '../../contexts/PanelActiveContext'
import { ADT_LIVE_COALESCE_MS } from '../../hooks/useAdtBoardLive'
import BedBoardPanel from './BedBoardPanel'
import BoardView from './adt/BoardView'
import type { AdtViewEntry } from './adt/viewRegistry'

const handlers: Record<string, Array<(payload?: unknown) => void>> = {}

function fire(event: string, payload?: unknown) {
  for (const fn of [...(handlers[event] ?? [])]) fn(payload)
}

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    socket: {
      connected: true,
      on: (event: string, fn: (payload?: unknown) => void) => {
        (handlers[event] ??= []).push(fn)
      },
      off: (event: string, fn: (payload?: unknown) => void) => {
        handlers[event] = (handlers[event] ?? []).filter((h) => h !== fn)
      },
    },
  }
})

vi.mock('../../api/adt', async () => {
  const actual = await vi.importActual<typeof import('../../api/adt')>('../../api/adt')
  return { ...actual, getAdtMeta: vi.fn() }
})

vi.mock('../../api/adt/board', async () => {
  const actual = await vi.importActual<typeof import('../../api/adt/board')>('../../api/adt/board')
  return { ...actual, getBedBoard: vi.fn() }
})

vi.mock('../../components/AdmitPatientModal', () => ({
  default: (props: { clerk?: boolean; onClose: () => void }) => (
    <div role="dialog" aria-label="Admission form" data-clerk={String(!!props.clerk)}>
      <button type="button" onClick={props.onClose}>Close the form</button>
    </div>
  ),
}))

const adtApi = await import('../../api/adt')
const boardApi = await import('../../api/adt/board')
const getMeta = vi.mocked(adtApi.getAdtMeta)
const getBoard = vi.mocked(boardApi.getBedBoard)

beforeAll(async () => {
  await i18n.changeLanguage('en')
})

const BOARD: BedBoard = {
  generated_at: '2026-10-10T10:00:00.000000+00:00',
  department: 'General Medicine',
  ward_id: null,
  ward_choices: [
    { id: 'WRD-1', code: 'GM-1', name: null, department: 'General Medicine', is_active: true },
    { id: 'WRD-2', code: 'GM-2', name: null, department: 'General Medicine', is_active: true },
  ],
  status_words: { available: 'Available' },
  wards: [{
    id: 'WRD-1', code: 'GM-1', name: null, department: 'General Medicine', ward_type: 'general',
    is_active: true, cleaning_tracked: false, register_complete: true,
    counts: { beds: 0, free: 0, occupied_total: 0 }, tiles: [],
  }],
}

const BOARD_ONLY: AdtViewEntry[] = [{
  id: 'board', label: 'Board', order: 10, permission: null, Component: BoardView,
  source: './adt/BoardView.tsx',
}]

async function settle() {
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
}

async function passCoalesceWindow() {
  await act(async () => { vi.advanceTimersByTime(ADT_LIVE_COALESCE_MS + 10) })
  await settle()
}

async function renderBoard(role = 'doctor') {
  render(<BedBoardPanel views={BOARD_ONLY} holds={() => true} role={role} />)
  await settle()
  await settle()
  expect(getBoard).toHaveBeenCalledTimes(1)
}

beforeEach(() => {
  vi.useFakeTimers()
  for (const key of Object.keys(handlers)) delete handlers[key]
  getMeta.mockReset()
  getMeta.mockResolvedValue({ module: 'adt', state: 'on' })
  getBoard.mockReset()
  getBoard.mockResolvedValue(BOARD)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the live bed board', () => {
  it('re-reads the board once for one event', async () => {
    await renderBoard()
    act(() => { fire('adt:changed', { ward_id: 'WRD-1' }) })
    expect(getBoard).toHaveBeenCalledTimes(1)
    await passCoalesceWindow()
    expect(getBoard).toHaveBeenCalledTimes(2)
    expect(getBoard).toHaveBeenLastCalledWith(null)
  })

  it('reads a burst of events (a move between two wards) once', async () => {
    await renderBoard()
    act(() => {
      fire('adt:changed', { ward_id: 'WRD-1' })
      fire('adt:changed', { ward_id: 'WRD-2' })
    })
    await passCoalesceWindow()
    expect(getBoard).toHaveBeenCalledTimes(2)
    act(() => { fire('adt:changed', { ward_id: 'WRD-2' }) })
    await passCoalesceWindow()
    expect(getBoard).toHaveBeenCalledTimes(3)
  })

  it('takes the ward from the event and nothing else', async () => {
    const { subscribeAdtChanged } = await import('../../api/adt/live')
    const seen: unknown[] = []
    const off = subscribeAdtChanged((event) => seen.push(event))
    fire('adt:changed', { ward_id: 'WRD-1', patient_id: 'P1', name: 'Asha Rao', bed: '4' })
    fire('adt:changed', { patient_id: 'P2' })
    off()
    expect(seen).toEqual([{ ward_id: 'WRD-1' }, { ward_id: null }])
  })

  it('ignores another ward while one ward is on screen, and re-reads for no ward', async () => {
    await renderBoard()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'WRD-2' } })
    await settle()
    const before = getBoard.mock.calls.length
    expect(getBoard).toHaveBeenLastCalledWith('WRD-2')
    act(() => { fire('adt:changed', { ward_id: 'WRD-1' }) })
    await passCoalesceWindow()
    expect(getBoard.mock.calls.length).toBe(before)
    act(() => { fire('adt:changed', { ward_id: 'WRD-2' }) })
    await passCoalesceWindow()
    expect(getBoard.mock.calls.length).toBe(before + 1)
    act(() => { fire('adt:changed', { ward_id: null }) })
    await passCoalesceWindow()
    expect(getBoard.mock.calls.length).toBe(before + 2)
  })

  it('falls back to polling while the socket is down and re-reads once when it is back', async () => {
    await renderBoard()
    act(() => { fire('disconnect') })
    await act(async () => { vi.advanceTimersByTime(30_000) })
    await settle()
    expect(getBoard).toHaveBeenCalledTimes(2)
    act(() => { fire('connect') })
    await passCoalesceWindow()
    expect(getBoard).toHaveBeenCalledTimes(3)
    // A second connect with no drop in between is not a reconnect.
    act(() => { fire('connect') })
    await passCoalesceWindow()
    expect(getBoard).toHaveBeenCalledTimes(3)
  })

  it('keeps polling every 30 seconds with the socket up, too', async () => {
    await renderBoard()
    await act(async () => { vi.advanceTimersByTime(30_000) })
    await settle()
    expect(getBoard).toHaveBeenCalledTimes(2)
  })

  it('re-reads at once when this screen announces a change', async () => {
    await renderBoard()
    act(() => { announceDataChanged() })
    await passCoalesceWindow()
    expect(getBoard).toHaveBeenCalledTimes(2)
  })

  it('stops listening when the board is closed', async () => {
    const view = render(<BedBoardPanel views={BOARD_ONLY} holds={() => true} role="doctor" />)
    await settle()
    await settle()
    view.unmount()
    expect(handlers['adt:changed'] ?? []).toHaveLength(0)
  })
})

describe("the ward clerk's admission button", () => {
  it('is offered to the ward clerk and opens the registration-only form', async () => {
    await renderBoard('ward_clerk')
    fireEvent.click(screen.getByRole('button', { name: 'Admit a patient' }))
    expect(screen.getByRole('dialog', { name: 'Admission form' })).toHaveAttribute('data-clerk', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'Close the form' }))
    expect(screen.queryByRole('dialog', { name: 'Admission form' })).not.toBeInTheDocument()
  })

  it('is not offered to anyone else', async () => {
    for (const role of ['doctor', 'admin', 'nurse']) {
      const view = render(<BedBoardPanel views={BOARD_ONLY} holds={() => true} role={role} />)
      await settle()
      expect(screen.queryByRole('button', { name: 'Admit a patient' })).not.toBeInTheDocument()
      view.unmount()
    }
  })
})
