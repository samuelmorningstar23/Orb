/**
 * HousekeepingPanel — the five things this screen must never get wrong.
 *
 * 1. THE CAPACITY SENTENCE IS ON SCREEN BEFORE THE NETWORK ANSWERS, AND STILL
 *    THERE WHEN EVERY REQUEST FAILS. Orb's capacity screen counts an available
 *    bed as "ward bed count − admitted patients" and knows nothing about dirt.
 *    A reader who does not know that these are two different numbers will offer
 *    a bed that has the last patient's linen on it. The one message that must
 *    never be missing cannot be one that depends on the network being up.
 *
 * 2. READINESS IS NEVER SHOWN WITHOUT COVERAGE. "3 ready" on a twelve-bed ward
 *    with three registered beds is not "three beds free". The pair is the fact;
 *    either number alone is a misreading waiting to happen.
 *
 * 3. A DISABLED MODULE IS NOT AN EMPTY BOARD. An empty cleaning board is a
 *    statement about the hospital ("nothing needs cleaning"). "Not switched on
 *    at this site" is a statement about the software.
 *
 * 4. A TERMINAL CLEAN IS DISTINCT IN WORDS, NOT ONLY IN COLOUR. It is a
 *    different job with different PPE and a longer expectation, and some of the
 *    people reading this board across a corridor do not distinguish red from
 *    green.
 *
 * 5. THE BUTTONS ARE THE SERVER'S STATE MACHINE. A dirty bed offers "Cleaning in
 *    progress" and never "Ready" — and it offers it because the SERVER said so,
 *    not because this file has its own copy of the transition table.
 *
 * PROVEN ABLE TO FAIL (2026-08-12): each assertion was watched go red with its
 * defect reintroduced — the CAPACITY_NOTE literal replaced by the server-only
 * value, the coverage cell removed from the readiness row, the
 * `isModuleDisabled` branch deleted, the TerminalTag replaced by a bare colour
 * class, and the transition buttons hard-coded to all five states — then
 * restored.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import HousekeepingPanel from './HousekeepingPanel'
import { formatMinutes, HousekeepingApiError } from '../../api/housekeeping'

vi.mock('../../api/housekeeping', async () => {
  const actual = await vi.importActual<typeof import('../../api/housekeeping')>(
    '../../api/housekeeping',
  )
  return {
    ...actual,
    getMeta: vi.fn(),
    getOverview: vi.fn(),
    getUnits: vi.fn(),
    getTasks: vi.fn(),
    getTurnaround: vi.fn(),
    getLinen: vi.fn(),
    createUnit: vi.fn(),
    setUnitState: vi.fn(),
    recordOccupied: vi.fn(),
    cancelRequest: vi.fn(),
  }
})

const api = await import('../../api/housekeeping')

const META = {
  states: ['occupied', 'vacant_dirty', 'cleaning_in_progress', 'vacant_ready', 'out_of_service'],
  legal_transitions: {
    occupied: ['out_of_service', 'vacant_dirty'],
    vacant_dirty: ['cleaning_in_progress', 'out_of_service'],
    cleaning_in_progress: ['out_of_service', 'vacant_dirty', 'vacant_ready'],
    vacant_ready: ['occupied', 'out_of_service', 'vacant_dirty'],
    out_of_service: ['vacant_dirty'],
  },
  request_types: ['discharge_turnaround', 'spillage', 'routine'],
  priorities: ['routine', 'urgent', 'immediate'],
  clean_types: ['standard', 'terminal'],
  linen_directions: ['issue', 'return_soiled', 'return_clean', 'condemned'],
  linen_items: [{ code: 'BEDSHEET', name: 'Bed sheet', is_active: true }],
  expected_minutes: { standard: 45, terminal: 120 },
} as unknown as Awaited<ReturnType<typeof api.getMeta>>

function task(over: Record<string, unknown> = {}) {
  return {
    id: 'HKT1', unit_id: 'U1', unit_code: 'GM-1/03', ward: 'GM-1',
    request_type: 'discharge_turnaround', priority: 'routine', clean_type: 'standard',
    expected_minutes: 45, status: 'open', source_type: 'discharge',
    description: null, precaution_note: null,
    vacated_at: '2026-08-12T04:00:00+00:00', requested_at: '2026-08-12T04:00:00+00:00',
    started_at: null, completed_at: null, ready_at: null, cancel_reason: null,
    turnaround_minutes: null, cleaning_minutes: null, is_terminal_clean: false,
    elapsed_minutes: 30, minutes_over_expected: 0, is_overdue: false,
    ...over,
  }
}

const READINESS = {
  wards: [{
    ward: 'GM-1', registered_units: 3, ward_capacity: 12, coverage_pct: 25,
    occupied: 0, vacant_dirty: 2, cleaning_in_progress: 0, vacant_ready: 1,
    out_of_service: 0, not_ready_registered: 2,
    capacity_view_available: 9, capacity_view_admitted: 3,
  }],
  capacity_basis:
    'The capacity and forecast screens compute available beds as the ward’s bed count '
    + '(a site setting) minus admitted patients. They do NOT subtract beds awaiting cleaning, '
    + 'and this module does not change them.',
  reconciliation_note:
    'Compare capacity_view_available against not_ready_registered — and read both '
    + 'against coverage_pct.',
}

function overview(over: Record<string, unknown> = {}) {
  return {
    counts: { open: 2, in_progress: 1, overdue: 1, terminal_open: 1, immediate: 0 },
    queue: [task()],
    readiness: READINESS,
    discharge_sweep: { created: 0, already_present: 2, unmatched: 0, unmatched_wards: {}, scanned: 2, lookback_hours: 72 },
    expected_minutes: { standard: 45, terminal: 120 },
    ...over,
  } as unknown as Awaited<ReturnType<typeof api.getOverview>>
}

function happyPath(over: { units?: unknown[]; tasks?: unknown[]; overview?: Record<string, unknown> } = {}) {
  vi.mocked(api.getMeta).mockResolvedValue(META)
  vi.mocked(api.getOverview).mockResolvedValue(overview(over.overview))
  vi.mocked(api.getUnits).mockResolvedValue(
    (over.units ?? []) as Awaited<ReturnType<typeof api.getUnits>>,
  )
  vi.mocked(api.getTasks).mockResolvedValue(
    (over.tasks ?? [task()]) as Awaited<ReturnType<typeof api.getTasks>>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

/* ═══════════════════════════════════════════════════════════════════════════ */

describe('the capacity boundary', () => {
  it('states the capacity relationship before any request resolves', () => {
    // Deliberately never-resolving promises: this is the first paint.
    vi.mocked(api.getMeta).mockReturnValue(new Promise(() => {}))
    vi.mocked(api.getOverview).mockReturnValue(new Promise(() => {}))
    vi.mocked(api.getUnits).mockReturnValue(new Promise(() => {}))
    vi.mocked(api.getTasks).mockReturnValue(new Promise(() => {}))

    render(<HousekeepingPanel />)
    expect(
      screen.getByText(/do not subtract beds awaiting cleaning/i),
    ).toBeInTheDocument()
  })

  it('keeps the capacity sentence when every request fails', async () => {
    const boom = new Error('network down')
    vi.mocked(api.getMeta).mockRejectedValue(boom)
    vi.mocked(api.getOverview).mockRejectedValue(boom)
    vi.mocked(api.getUnits).mockRejectedValue(boom)
    vi.mocked(api.getTasks).mockRejectedValue(boom)

    render(<HousekeepingPanel />)
    await screen.findByRole('alert')
    expect(
      screen.getByText(/do not subtract beds awaiting cleaning/i),
    ).toBeInTheDocument()
  })

  it('never labels its own figures "available"', async () => {
    happyPath()
    render(<HousekeepingPanel />)
    await screen.findByRole('button', { name: 'Ward readiness' })
    fireEvent.click(screen.getByRole('button', { name: 'Ward readiness' }))

    // The only "available" on this screen is explicitly the capacity screen's.
    expect(await screen.findByText(/Capacity screen says available/i)).toBeInTheDocument()
  })
})

describe('readiness is never shown without coverage', () => {
  it('renders registered-of-capacity and coverage beside the ready count', async () => {
    happyPath()
    render(<HousekeepingPanel />)
    await screen.findByRole('button', { name: 'Ward readiness' })
    fireEvent.click(screen.getByRole('button', { name: 'Ward readiness' }))

    const row = await screen.findByRole('row', { name: /GM-1/ })
    const cells = within(row).getAllByRole('cell')
    // 3 of 12 registered, 25% coverage — the pair that stops "1 ready" being
    // read as "one bed free on a twelve-bed ward".
    expect(within(row).getByText(/of 12/)).toBeInTheDocument()
    expect(within(row).getByText('25%')).toBeInTheDocument()
    expect(cells.some(c => c.textContent?.includes('9'))).toBe(true)
    expect(within(row).getByText(/not yet usable/i)).toBeInTheDocument()
  })

  it('says Unknown, not zero, when a ward has no configured bed count', async () => {
    happyPath({
      overview: {
        readiness: {
          ...READINESS,
          wards: [{
            ...READINESS.wards[0], ward_capacity: null, coverage_pct: null,
            capacity_view_available: null,
          }],
        },
      },
    })
    render(<HousekeepingPanel />)
    await screen.findByRole('button', { name: 'Ward readiness' })
    fireEvent.click(screen.getByRole('button', { name: 'Ward readiness' }))

    const row = await screen.findByRole('row', { name: /GM-1/ })
    expect(within(row).getAllByText('Unknown').length).toBeGreaterThanOrEqual(2)
    expect(within(row).queryByText(/of 0/)).not.toBeInTheDocument()
  })
})

describe('a switched-off module', () => {
  it('says the module is off rather than showing an empty cleaning board', async () => {
    const off = new HousekeepingApiError('Housekeeping module is disabled', 404, 'module_disabled')
    vi.mocked(api.getMeta).mockRejectedValue(off)
    vi.mocked(api.getOverview).mockRejectedValue(off)
    vi.mocked(api.getUnits).mockRejectedValue(off)
    vi.mocked(api.getTasks).mockRejectedValue(off)

    render(<HousekeepingPanel />)
    expect(
      await screen.findByText(/Housekeeping is not switched on at this site/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Nothing is waiting/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Waiting to be cleaned/i)).not.toBeInTheDocument()
  })
})

describe('a terminal clean', () => {
  it('is labelled in words, not only in colour', async () => {
    happyPath({
      tasks: [task({ id: 'HKT-T', is_terminal_clean: true, clean_type: 'terminal', expected_minutes: 120 })],
    })
    render(<HousekeepingPanel />)
    const row = await screen.findByRole('row', { name: /GM-1\/03/ })
    expect(within(row).getByText('Terminal clean')).toBeInTheDocument()
  })

  it('carries the longer expected turnaround on the board', async () => {
    happyPath({
      tasks: [task({ is_terminal_clean: true, clean_type: 'terminal', expected_minutes: 120 })],
    })
    render(<HousekeepingPanel />)
    const row = await screen.findByRole('row', { name: /GM-1\/03/ })
    expect(within(row).getByText('2h')).toBeInTheDocument()
  })
})

describe('the state machine belongs to the server', () => {
  it('offers a dirty bed only the moves the server calls legal', async () => {
    happyPath({
      units: [{
        id: 'U1', unit_code: 'GM-1/03', ward: 'GM-1', room: null, bed_label: '03',
        unit_type: 'bed', is_isolation_room: false, state: 'vacant_dirty',
        state_since: null, is_active: true, notes: null, minutes_in_state: 20,
      }],
    })
    render(<HousekeepingPanel />)
    await screen.findByRole('button', { name: 'Beds & rooms' })
    fireEvent.click(screen.getByRole('button', { name: 'Beds & rooms' }))

    const row = await screen.findByRole('row', { name: /GM-1\/03/ })
    expect(within(row).getByRole('button', { name: 'Cleaning in progress' })).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Out of service' })).toBeInTheDocument()
    // A dirty bed cannot be declared ready. The button must not exist.
    expect(within(row).queryByRole('button', { name: 'Ready' })).not.toBeInTheDocument()
    // …and occupying it is offered only with the words that say what it means.
    expect(
      within(row).getByRole('button', { name: 'Occupied (clean skipped)' }),
    ).toBeInTheDocument()
  })

  it('offers a ready bed a plain Occupied action, with no skipped-clean warning', async () => {
    happyPath({
      units: [{
        id: 'U2', unit_code: 'GM-1/04', ward: 'GM-1', room: null, bed_label: '04',
        unit_type: 'bed', is_isolation_room: false, state: 'vacant_ready',
        state_since: null, is_active: true, notes: null, minutes_in_state: 5,
      }],
    })
    render(<HousekeepingPanel />)
    await screen.findByRole('button', { name: 'Beds & rooms' })
    fireEvent.click(screen.getByRole('button', { name: 'Beds & rooms' }))

    const row = await screen.findByRole('row', { name: /GM-1\/04/ })
    expect(within(row).getByRole('button', { name: 'Occupied' })).toBeInTheDocument()
    expect(
      within(row).queryByRole('button', { name: /clean skipped/ }),
    ).not.toBeInTheDocument()
  })
})

describe('discharges the sweep could not place', () => {
  it('says so instead of silently raising nothing', async () => {
    happyPath({
      overview: {
        discharge_sweep: {
          created: 0, already_present: 0, unmatched: 4,
          unmatched_wards: { 'GM-1': 4 }, scanned: 4, lookback_hours: 72,
        },
      },
    })
    render(<HousekeepingPanel />)
    expect(
      await screen.findByText(/4 recent discharges could not be matched to a registered bed/i),
    ).toBeInTheDocument()
  })
})

describe('counts that used to disagree', () => {
  it('captions open requests as requests, and counts dirty beds no request covers', async () => {
    happyPath({ overview: { counts: { open: 2, in_progress: 1, overdue: 0, terminal_open: 0, immediate: 0, dirty_without_request: 3 } } })
    render(<HousekeepingPanel />)
    expect(await screen.findByText('Open cleaning requests')).toBeInTheDocument()
    const dirty = screen.getByText('Dirty beds with no request')
    expect(dirty.parentElement).toHaveTextContent('3')
  })

  it('explains more beds registered than the bed count instead of printing over 100%', async () => {
    const note = '14 beds are registered here but the ward\'s bed count is 12. Check the bed count setting, or look for a bed registered twice.'
    happyPath({
      overview: {
        readiness: {
          ...READINESS,
          wards: [{ ...READINESS.wards[0], registered_units: 14, coverage_pct: 100, over_capacity: true, coverage_note: note }],
        },
      },
    })
    render(<HousekeepingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Ward readiness' }))
    const row = await screen.findByRole('row', { name: /GM-1/ })
    expect(within(row).getByText(note)).toBeInTheDocument()
    expect(within(row).queryByText('100%')).not.toBeInTheDocument()
  })
})

describe('formatMinutes', () => {
  it('renders an unmeasured turnaround as an em dash, never as 0m', () => {
    // A turnaround of zero reads as excellent. An unmeasured one must never
    // borrow that reading.
    expect(formatMinutes(null)).toBe('—')
    expect(formatMinutes(undefined)).toBe('—')
    expect(formatMinutes(0)).toBe('0m')
    expect(formatMinutes(45)).toBe('45m')
    expect(formatMinutes(155)).toBe('2h 35m')
    expect(formatMinutes(120)).toBe('2h')
    // Past a day the reader gets days, not "669h 5m".
    expect(formatMinutes(40145)).toBe('27d 21h')
    expect(formatMinutes(1440)).toBe('1d')
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   6. A bed is registered on one of the hospital's wards
   A free-text Ward field let 'gm-1' typed beside 'GM-1' create a phantom ward.
   ═══════════════════════════════════════════════════════════════════════════ */

describe('registering a bed', () => {
  it('chooses the ward from the hospital list and sends that ward', async () => {
    happyPath({ units: [{
      id: 'U9', unit_code: 'CCU/01', ward: 'CCU', room: null, bed_label: '01',
      unit_type: 'bed', is_isolation_room: false, state: 'vacant_ready',
      state_since: '2026-08-12T04:00:00+00:00', notes: null, is_active: true,
      minutes_in_state: 10,
    }] })
    vi.mocked(api.createUnit).mockResolvedValue({} as never)

    render(<HousekeepingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Beds & rooms' }))
    const form = (await screen.findByRole('button', { name: 'Register bed' })).closest('form')!
    const ward = within(form).getByLabelText('Ward') as HTMLSelectElement
    expect(ward.tagName).toBe('SELECT')
    expect(within(ward).getAllByRole('option').map(o => o.textContent))
      .toEqual(['Choose a ward', 'CCU', 'GM-1'])

    fireEvent.change(within(form).getByLabelText('Unit code'), { target: { value: 'GM-1/07' } })
    fireEvent.change(ward, { target: { value: 'GM-1' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Register bed' }))
    expect(api.createUnit).toHaveBeenCalledWith(expect.objectContaining({
      unit_code: 'GM-1/07', ward: 'GM-1',
    }))
  })
})


/* ═══════════════════════════════════════════════════════════════════════════
   7. Nothing changes a bed on one tap, and nothing asks through window.prompt
   An occupied bed was marked vacant by a single click, and the two reasons
   the server needs were asked for in a browser prompt.
   ═══════════════════════════════════════════════════════════════════════════ */

const OCCUPIED = {
  id: 'U5', unit_code: 'GM-1/05', ward: 'GM-1', room: null, bed_label: '05',
  unit_type: 'bed', is_isolation_room: false, state: 'occupied',
  state_since: null, is_active: true, notes: null, minutes_in_state: 300,
}

describe('changing a bed asks first', () => {
  it('names the bed and the move, needs a reason to vacate an occupied bed, then sends it', async () => {
    happyPath({ units: [OCCUPIED] })
    vi.mocked(api.setUnitState).mockResolvedValue({} as never)
    render(<HousekeepingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Beds & rooms' }))
    const row = await screen.findByRole('row', { name: /GM-1\/05/ })
    fireEvent.click(within(row).getByRole('button', { name: 'Vacant: needs cleaning' }))

    expect(api.setUnitState).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: 'Change GM-1/05' })
    expect(dialog.textContent).toContain('GM-1/05, bed 05, GM-1')
    expect(dialog.textContent).toContain('Occupied')
    const go = within(dialog).getByRole('button', { name: 'Move to Vacant: needs cleaning' })
    expect(go).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Patient transferred to CCU' } })
    await act(async () => { fireEvent.click(go) })
    expect(api.setUnitState).toHaveBeenCalledWith('U5', 'vacant_dirty', 'Patient transferred to CCU')
  })

  it('asks for the skipped-clean reason in a drawer, not a browser prompt', async () => {
    const prompt = vi.spyOn(window, 'prompt')
    happyPath({ units: [{ ...OCCUPIED, id: 'U6', unit_code: 'GM-1/06', bed_label: '06', state: 'vacant_dirty' }] })
    vi.mocked(api.recordOccupied).mockResolvedValue({} as never)
    render(<HousekeepingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Beds & rooms' }))
    const row = await screen.findByRole('row', { name: /GM-1\/06/ })
    fireEvent.click(within(row).getByRole('button', { name: 'Occupied (clean skipped)' }))
    expect(prompt).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: 'Occupy GM-1/06 without a clean' })
    fireEvent.change(within(dialog).getByLabelText('Why is it being occupied without a clean?'),
      { target: { value: 'Emergency admission' } })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Record occupied' }))
    })
    expect(api.recordOccupied).toHaveBeenCalledWith('U6', 'Emergency admission')
    prompt.mockRestore()
  })

  it('cancels a cleaning request only with a reason given in a drawer', async () => {
    const prompt = vi.spyOn(window, 'prompt')
    happyPath()
    vi.mocked(api.cancelRequest).mockResolvedValue({} as never)
    render(<HousekeepingPanel />)
    const row = await screen.findByRole('row', { name: /GM-1\/03/ })
    fireEvent.click(within(row).getByRole('button', { name: 'Cancel' }))
    expect(prompt).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: 'Cancel cleaning request' })
    expect(dialog.textContent).toContain('GM-1/03, GM-1')
    fireEvent.change(within(dialog).getByLabelText('Why is this request being cancelled?'),
      { target: { value: 'Raised twice' } })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel request' }))
    })
    expect(api.cancelRequest).toHaveBeenCalledWith('HKT1', 'Raised twice')
    prompt.mockRestore()
  })
})

describe('registering a unit code already on the register', () => {
  it('sends create_only and shows the refusal beside the form, keeping what was typed', async () => {
    happyPath({ units: [OCCUPIED] })
    vi.mocked(api.createUnit).mockRejectedValue(new HousekeepingApiError(
      'GM-1/05 is already registered (GM-1, bed 05). Nothing was changed.', 409, 'already_exists'))
    render(<HousekeepingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Beds & rooms' }))
    const form = (await screen.findByRole('button', { name: 'Register bed' })).closest('form')!
    fireEvent.change(within(form).getByLabelText('Unit code'), { target: { value: 'GM-1/05' } })
    fireEvent.change(within(form).getByLabelText('Ward'), { target: { value: 'GM-1' } })
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: 'Register bed' }))
    })
    expect(api.createUnit).toHaveBeenCalledWith(expect.objectContaining({ create_only: true }))
    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toContain('GM-1/05 is already registered'))
    expect((within(form).getByLabelText('Unit code') as HTMLInputElement).value).toBe('GM-1/05')
  })
})
