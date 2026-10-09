/**
 * Front Desk, Appointments (opd-16): what the desk must read and do correctly.
 *
 *  1. With outpatients switched off the view says so and asks the server for
 *     nothing; a 'feature_disabled' for outpatients keeps the other desk views
 *     (only a registration one hands back to the panel).
 *  2. Each clinic's slots render as clinic times with Free, Full or Started in
 *     words; a closed slot is a class, never opacity.
 *  3. A booking shows its doctor, its status in plain words, a computed
 *     no-show as "Did not come (clinic ended)", and the token once checked in.
 *  4. Booking a free slot sends the doctor, the day, the slot time and a
 *     request key; check-in goes through the identity check and sends what
 *     the patient stated.
 *  5. An account that cannot book sees no slot buttons and no actions.
 *
 * Driven through a mocked apiFetch, so the real src/api/appointmentsDesk.ts
 * and src/api/registration.ts build the URLs.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetFeatureFlags, setFeatureFlags } from '../../../shellConfig'
import AppointmentsDayView from './AppointmentsDayView'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
}))

const SLOT = (start: string, extra: Record<string, unknown> = {}) => ({
  start, end: start.replace(':00', ':30'), starts_at: '2026-10-05T03:30:00.000000+00:00',
  ends_at: '2026-10-05T04:00:00.000000+00:00', past: false, capacity: 1, booked: 0, free: 1,
  ...extra,
})

function appointment(extra: Record<string, unknown> = {}) {
  return {
    id: 'AP1', patient_id: 'PAT-1', patient_name: 'Meena Rao', patient_uhid: 'UH-77',
    patient_age: 46, patient_sex: 'F', inpatient_now: false, scheduled_date: '2026-10-05',
    scheduled_time: '10:00', appointment_type: 'Follow-up', appointment_type_key: 'follow_up',
    appointment_type_label: 'Follow-up', reason: null, status: 'booked',
    effective_status: 'booked', no_show_computed: false, origin: 'staff',
    department: 'Medicine', doctor_id: 'DOC-1', doctor_name: 'Dr Iyer', session_id: 'CS1',
    slot_position: 1, checked_in_at: null, opd_visit_id: null, token_number: null,
    visit_status: null, no_show_at: null, cancel_reason: null, cancelled_at: null,
    completed_at: null, rescheduled_from_id: null, created_at: '2026-10-04T10:00:00Z',
    ...extra,
  }
}

function day(extra: Record<string, unknown> = {}) {
  return {
    date: '2026-10-05', hospital_today: '2026-10-05', department: 'Medicine',
    appointments: [appointment()],
    sessions: [{
      session_id: 'CS1', doctor_id: 'DOC-1', doctor_name: 'Dr Iyer', department: 'Medicine',
      room: 'Room 2', start_time: '09:00', end_time: '11:00', slot_minutes: 30,
      capacity_per_slot: 1, cancelled: false, cancel_reason: null,
      slots: [SLOT('09:00', { past: true }), SLOT('10:00', { booked: 1, free: 0 }), SLOT('10:30')],
    }],
    types: [{ key: 'follow_up', label: 'Follow-up' }, { key: 'other', label: 'Other' }],
    ...extra,
  }
}

function routeApi(overrides: Record<string, unknown> = {}) {
  apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) {
        return typeof value === 'function'
          ? (value as (p: string, o?: RequestInit) => unknown)(path, options) : value
      }
    }
    if (path.startsWith('/appointments/day')) return day()
    throw new Error(`unrouted ${path}`)
  })
}

const noop = () => {}

function renderView(props: Partial<{ canBook: boolean; onFeatureOff: () => void }> = {}) {
  return render(
    <AppointmentsDayView
      scopedDepartment="Medicine"
      openView={noop}
      onFeatureOff={props.onFeatureOff ?? noop}
      canBook={props.canBook ?? true}
    />,
  )
}

beforeEach(() => {
  apiFetch.mockReset()
  setFeatureFlags({ registration: true, opd: true })
})
afterEach(() => {
  resetFeatureFlags()
  vi.restoreAllMocks()
})

describe('AppointmentsDayView', () => {
  it('says outpatients are switched off and asks for nothing', async () => {
    setFeatureFlags({ opd: false })
    routeApi()
    renderView()
    expect(screen.getByText(/switched off on this site/)).toBeInTheDocument()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('keeps the other desk views when only outpatients went off', async () => {
    const onFeatureOff = vi.fn()
    routeApi({
      '/appointments/day': () => {
        throw Object.assign(new Error('Switched off'), {
          status: 404, detail: { error: 'feature_disabled', feature: 'opd' } })
      },
    })
    renderView({ onFeatureOff })
    expect(await screen.findByText(/switched off on this site/)).toBeInTheDocument()
    expect(onFeatureOff).not.toHaveBeenCalled()
  })

  it('hands back to the panel when registration went off', async () => {
    const onFeatureOff = vi.fn()
    routeApi({
      '/appointments/day': () => {
        throw Object.assign(new Error('Switched off'), {
          status: 404, detail: { error: 'feature_disabled', feature: 'registration' } })
      },
    })
    renderView({ onFeatureOff })
    await waitFor(() => expect(onFeatureOff).toHaveBeenCalled())
  })

  it('shows the slots as clinic times with words, and a closed slot without opacity', async () => {
    routeApi()
    renderView()
    const slots = await screen.findByRole('list', { name: 'Slots with Dr Iyer' })
    const [started, full, free] = Array.from(slots.querySelectorAll('li'))
    expect(started).toHaveTextContent('09:00 AM')
    expect(started).toHaveTextContent('Started')
    expect(full).toHaveTextContent('Full')
    expect(free).toHaveTextContent('Free')
    expect(started.querySelector('.adv-slot--closed')).not.toBeNull()
    expect(started.innerHTML).not.toContain('opacity')
    expect(within(free).getByRole('button', { name: 'Book 10:30 AM, Free' })).toBeInTheDocument()
    expect(within(full).queryByRole('button')).toBeNull()
    // Asked with no date: the server picks the hospital's today.
    expect(apiFetch.mock.calls[0][0]).toBe('/appointments/day')
  })

  it('lists each booking with its doctor, its status in words and its token', async () => {
    routeApi({ '/appointments/day': day({ appointments: [
      appointment(),
      appointment({ id: 'AP2', patient_name: 'Ravi K', status: 'booked',
                    effective_status: 'no_show', no_show_computed: true, scheduled_time: '09:00' }),
      appointment({ id: 'AP3', patient_name: 'Asha P', status: 'checked_in',
                    effective_status: 'checked_in', token_number: 7, opd_visit_id: 'OPD-1' }),
    ] }) })
    renderView()
    const table = await screen.findByRole('table')
    expect(table).toHaveTextContent('Meena Rao')
    expect(table).toHaveTextContent('UHID UH-77')
    expect(table).toHaveTextContent('Dr Iyer')
    expect(table).toHaveTextContent('Did not come (clinic ended)')
    expect(table).toHaveTextContent('Checked in')
    expect(table).toHaveTextContent('Token 7')
    // A checked-in patient is not offered check-in again.
    const rows = within(table).getAllByRole('row')
    const asha = rows.find(r => r.textContent?.includes('Asha P'))!
    expect(within(asha).queryByRole('button', { name: 'Check in' })).toBeNull()
  })

  it('books a free slot with the doctor, the day, the time and a request key', async () => {
    const posted: { path: string; options?: RequestInit }[] = []
    routeApi({
      '/registration/search': { results: [{
        patient_id: 'PAT-9', uhid: 'UH-9', name: 'Kiran Das', sex: 'M', age: 30,
        age_stated: false, phone_last4: '1234', last_visit_date: null, inpatient_now: false,
      }], truncated: false, kinds: ['name'] },
      '/appointments': (path: string, options?: RequestInit) => {
        if (path.startsWith('/appointments/day')) return day()
        posted.push({ path, options })
        return appointment({ id: 'AP9' })
      },
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Book 10:30 AM, Free' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('UHID, full phone number or name'),
      { target: { value: 'Kiran' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Search' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Choose Kiran Das, UHID UH-9' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Book' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    const sent = JSON.parse(String(posted[0].options?.body))
    expect(posted[0].path).toBe('/appointments')
    expect(sent).toMatchObject({ patient_id: 'PAT-9', scheduled_date: '2026-10-05',
                                 scheduled_time: '10:30', doctor_id: 'DOC-1',
                                 appointment_type: 'follow_up' })
    const headers = posted[0].options?.headers as Record<string, string>
    expect(headers['Idempotency-Key']).toMatch(/^appointment-/)
    expect(await screen.findByText('Appointment booked.')).toBeInTheDocument()
  })

  it('retries one patient with the same request key and another patient with a new one', async () => {
    const posted: { body: Record<string, unknown>; key: string }[] = []
    routeApi({
      '/registration/search': { results: [
        { patient_id: 'PAT-9', uhid: 'UH-9', name: 'Kiran Das', sex: 'M', age: 30,
          age_stated: false, phone_last4: '1234', last_visit_date: null, inpatient_now: false },
        { patient_id: 'PAT-8', uhid: 'UH-8', name: 'Asha Rao', sex: 'F', age: 52,
          age_stated: false, phone_last4: '5678', last_visit_date: null, inpatient_now: false },
      ], truncated: false, kinds: ['name'] },
      '/appointments': (path: string, options?: RequestInit) => {
        if (path.startsWith('/appointments/day')) return day()
        const headers = options?.headers as Record<string, string>
        posted.push({ body: JSON.parse(String(options?.body)), key: headers['Idempotency-Key'] })
        // The first two answers are lost on the way back.
        if (posted.length <= 2) throw new Error('The connection dropped. Try again.')
        return appointment({ id: 'AP9', patient_id: 'PAT-8' })
      },
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Book 10:30 AM, Free' }))
    const dialog = await screen.findByRole('dialog')
    const choose = async (typed: string, name: string) => {
      fireEvent.change(within(dialog).getByLabelText('UHID, full phone number or name'),
        { target: { value: typed } })
      fireEvent.click(within(dialog).getByRole('button', { name: 'Search' }))
      fireEvent.click(await within(dialog).findByRole('button', { name }))
    }
    await choose('Kiran', 'Choose Kiran Das, UHID UH-9')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Book' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('connection dropped')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Book' }))
    await waitFor(() => expect(posted).toHaveLength(2))
    // A retry for the same patient is the same request.
    expect(posted[1].key).toBe(posted[0].key)

    fireEvent.click(await within(dialog).findByRole('button', { name: 'Choose another patient' }))
    await choose('Asha', 'Choose Asha Rao, UHID UH-8')
    expect(within(dialog).queryByRole('alert')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Book' }))
    await waitFor(() => expect(posted).toHaveLength(3))
    expect(posted[2].body.patient_id).toBe('PAT-8')
    expect(posted[2].key).toMatch(/^appointment-/)
    expect(posted[2].key).not.toBe(posted[0].key)
    expect(await screen.findByText('Appointment booked.')).toBeInTheDocument()
  })

  it('checks a patient in through the identity check and shows the token', async () => {
    const checkedIn: unknown[] = []
    routeApi({
      '/registration/identity-check': { result: 'match', checked: ['name', 'date_of_birth'],
                                        checked_at: '2026-10-05T06:30:00Z' },
      '/appointments/AP1/check-in': (_p: string, options?: RequestInit) => {
        checkedIn.push(JSON.parse(String(options?.body)))
        return { appointment: appointment({ status: 'checked_in', token_number: 4 }),
                 visit: { id: 'OPD-1', token_number: 4 }, replayed: false }
      },
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Check in' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Their name, as they say it'),
      { target: { value: 'Meena Rao' } })
    fireEvent.change(within(dialog).getByLabelText('Date of birth they state'),
      { target: { value: '1980-02-11' } })
    fireEvent.change(within(dialog).getByLabelText('Last 4 digits of their phone'),
      { target: { value: '1234' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Continue' }))
    await waitFor(() => expect(checkedIn).toHaveLength(1))
    expect(checkedIn[0]).toEqual({ identity_check: {
      name: 'Meena Rao', date_of_birth: '1980-02-11', phone_last4: '1234' } })
    expect(await screen.findByText('Meena Rao is checked in. Token 4.')).toBeInTheDocument()
  })

  it('shows an account that cannot book no slot buttons and no actions', async () => {
    routeApi()
    renderView({ canBook: false })
    const slots = await screen.findByRole('list', { name: 'Slots with Dr Iyer' })
    expect(within(slots).queryByRole('button')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Check in' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Move' })).toBeNull()
  })
})
