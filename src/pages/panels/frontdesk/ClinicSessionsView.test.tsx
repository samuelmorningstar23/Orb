/**
 * Clinic timings (opd-15): what the desk must read correctly.
 *
 *  1. Slots come from the server in hospital time and render as clinical
 *     clock times; a slot that already started is marked, not faded.
 *  2. A clinic that is not running says so with its reason and offers no slot.
 *  3. Only an account that may change the timetable sees the forms; the desk
 *     reads.
 *  4. Adding a clinic sends exactly the fields the server expects.
 *  5. A 'feature_disabled' answer hands back to the panel instead of drawing
 *     an empty timetable.
 *
 * Driven through a mocked apiFetch, so the real src/api/schedule.ts builds the
 * URLs.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ClinicSessionsView from './ClinicSessionsView'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
}))

const DOCTORS = { doctors: [{ id: 'DOC-1', name: 'Dr Iyer', department: 'Cardiology' }] }

const SESSION = {
  id: 'CS1', doctor_id: 'DOC-1', doctor_name: 'Dr Iyer', doctor_active: true,
  department: 'Cardiology', weekday: 0, weekday_name: 'Monday', start_time: '09:00',
  end_time: '10:00', slot_minutes: 30, capacity_per_slot: 1, slot_count: 2, room: 'Room 4',
  valid_from: null, valid_to: null, is_active: true, notes: null, row_version: 1,
  created_by: 'ADM1', created_at: '2026-10-03T10:00:00.000000+00:00', updated_by: null,
  updated_at: null, retired_by: null, retired_at: null, retired_reason: null,
}

function availability(overrides: Record<string, unknown> = {}) {
  return {
    from: '2026-10-05', to: '2026-10-05', hospital_today: '2026-10-05', timezone: 'Asia/Kolkata',
    days: [{
      date: '2026-10-05', weekday: 0, weekday_name: 'Monday',
      sessions: [{
        session_id: 'CS1', doctor_id: 'DOC-1', doctor_name: 'Dr Iyer', department: 'Cardiology',
        room: 'Room 4', start_time: '09:00', end_time: '10:00', slot_minutes: 30,
        capacity_per_slot: 1, cancelled: false, cancel_reason: null, exception_id: null,
        slots: [
          { start: '09:00', end: '09:30', starts_at: '2026-10-05T03:30:00.000000+00:00',
            ends_at: '2026-10-05T04:00:00.000000+00:00', past: true, capacity: 1 },
          { start: '09:30', end: '10:00', starts_at: '2026-10-05T04:00:00.000000+00:00',
            ends_at: '2026-10-05T04:30:00.000000+00:00', past: false, capacity: 1 },
        ],
        ...overrides,
      }],
    }],
  }
}

function routeApi(overrides: Record<string, unknown> = {}) {
  apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) {
        return typeof value === 'function' ? (value as (p: string, o?: RequestInit) => unknown)(path, options) : value
      }
    }
    if (path.startsWith('/schedule/doctors')) return DOCTORS
    if (path.startsWith('/schedule/sessions')) return { sessions: [SESSION] }
    if (path.startsWith('/schedule/exceptions')) return { exceptions: [] }
    if (path.startsWith('/schedule/availability')) return availability()
    throw new Error(`unrouted ${path}`)
  })
}

const noop = () => {}

function renderView(props: Partial<{ canManage: boolean; onFeatureOff: () => void }> = {}) {
  return render(
    <ClinicSessionsView
      scopedDepartment={null}
      openView={noop}
      onFeatureOff={props.onFeatureOff ?? noop}
      canManage={props.canManage ?? false}
    />,
  )
}

beforeEach(() => { apiFetch.mockReset() })
afterEach(() => vi.restoreAllMocks())

describe('ClinicSessionsView', () => {
  it('shows the day the server says is today, with its slots as clinic times', async () => {
    routeApi()
    renderView()
    expect(await screen.findByText('Monday, 5 Oct 2026')).toBeInTheDocument()
    const slots = screen.getByRole('list', { name: 'Slots for Dr Iyer' })
    expect(slots).toHaveTextContent('09:00 AM')
    expect(slots).toHaveTextContent('09:30 AM')
    // Asked with no date: the server picks the hospital's today.
    expect(apiFetch.mock.calls.some(([p]) => p === '/schedule/availability')).toBe(true)
  })

  it('marks a slot that already started with a class and words, never opacity', async () => {
    routeApi()
    renderView()
    const slots = await screen.findByRole('list', { name: 'Slots for Dr Iyer' })
    const [first, second] = Array.from(slots.querySelectorAll('li'))
    expect(first.className).toContain('cs-slot--past')
    expect(first).toHaveTextContent('(started)')
    expect(second.className).not.toContain('cs-slot--past')
    expect(first.getAttribute('style') ?? '').not.toContain('opacity')
  })

  it('says a clinic is not running, with the reason, and offers no slot', async () => {
    routeApi({ '/schedule/availability': availability({
      cancelled: true, cancel_reason: 'On leave', slots: [], exception_id: 'CX1' }) })
    renderView()
    expect(await screen.findByText('Not running: On leave')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Slots for Dr Iyer' })).toBeNull()
  })

  it('lists the weekly clinics in words', async () => {
    routeApi()
    renderView()
    const table = await screen.findByRole('table')
    expect(table).toHaveTextContent('Monday')
    expect(table).toHaveTextContent('09:00 AM to 10:00 AM')
    expect(table).toHaveTextContent('2 slots of 30 min')
    expect(table).toHaveTextContent('Every week')
  })

  it('shows the desk no forms and no change buttons', async () => {
    routeApi()
    renderView({ canManage: false })
    await screen.findByRole('table')
    expect(screen.queryByText('Add a clinic')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retire' })).toBeNull()
    expect(screen.queryByText('Mark a clinic as not running')).toBeNull()
  })

  it('lets the administrator add a clinic and sends the server its fields', async () => {
    const posted: unknown[] = []
    routeApi({
      '/schedule/sessions': (path: string, options?: RequestInit) => {
        if (options?.method === 'POST') {
          posted.push(JSON.parse(String(options.body)))
          return SESSION
        }
        return { sessions: [SESSION] }
      },
    })
    renderView({ canManage: true })
    await screen.findByText('Add a clinic')
    fireEvent.change(screen.getByLabelText('Doctor', { selector: '#cs-new-doctor' }),
      { target: { value: 'DOC-1' } })
    fireEvent.change(screen.getByLabelText('Day', { selector: '#cs-new-weekday' }),
      { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Room', { selector: '#cs-new-room' }),
      { target: { value: 'Room 7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add clinic' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0]).toEqual({
      doctor_id: 'DOC-1', weekday: 2, start_time: '09:00', end_time: '13:00',
      slot_minutes: 15, capacity_per_slot: 1, room: 'Room 7', valid_from: null,
      valid_to: null, notes: null,
    })
  })

  it('shows a refusal from the server in its own words', async () => {
    routeApi({
      '/schedule/sessions': (path: string, options?: RequestInit) => {
        if (options?.method === 'POST') {
          throw Object.assign(new Error('Dr Iyer already holds a clinic on Monday from 09:00 to 10:00 (Cardiology). One doctor cannot hold two clinics at the same time.'), { status: 409 })
        }
        return { sessions: [SESSION] }
      },
    })
    renderView({ canManage: true })
    await screen.findByText('Add a clinic')
    fireEvent.change(screen.getByLabelText('Doctor', { selector: '#cs-new-doctor' }),
      { target: { value: 'DOC-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add clinic' }))
    expect(await screen.findByText(/cannot hold two clinics/)).toBeInTheDocument()
  })

  it('asks for a reason before retiring a clinic', async () => {
    const retired: unknown[] = []
    routeApi({
      '/schedule/sessions/CS1/retire': (path: string, options?: RequestInit) => {
        retired.push(JSON.parse(String(options?.body)))
        return { ...SESSION, is_active: false }
      },
    })
    renderView({ canManage: true })
    fireEvent.click(await screen.findByRole('button', { name: 'Retire' }))
    fireEvent.click(screen.getByRole('button', { name: 'Retire clinic' }))
    expect(await screen.findByText('Say why this clinic is being retired.')).toBeInTheDocument()
    expect(retired).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Reason for retiring this clinic'),
      { target: { value: 'Clinic moved' } })
    fireEvent.click(screen.getByRole('button', { name: 'Retire clinic' }))
    await waitFor(() => expect(retired).toEqual([{ reason: 'Clinic moved' }]))
  })

  it('hands back to the panel when the registration feature is off', async () => {
    const onFeatureOff = vi.fn()
    routeApi({
      '/schedule/availability': () => {
        throw Object.assign(new Error('The registration desk is switched off on this site.'),
          { status: 404, detail: { error: 'feature_disabled', feature: 'registration' } })
      },
    })
    renderView({ onFeatureOff })
    await waitFor(() => expect(onFeatureOff).toHaveBeenCalled())
    expect(await screen.findByText('Clinic timings are switched off on this site.')).toBeInTheDocument()
    expect(screen.queryByText('No clinic is held on this day.')).toBeNull()
  })

  it('a failed load reads as an error, never as an empty timetable', async () => {
    routeApi({ '/schedule/sessions': () => { throw new Error('Could not reach the server.') } })
    renderView()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server.')
    expect(screen.queryByText(/No clinic timings have been set up yet/)).toBeNull()
  })
})
