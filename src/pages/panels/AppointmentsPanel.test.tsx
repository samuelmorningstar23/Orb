/**
 * The Appointments board shows bookings, says so when there are none, and its
 * tiles count what their labels promise.
 *
 * F013: the server used to pad the list with a date-seeded random schedule
 * (source 'predicted') that the board rendered as bookings. Only booked rows
 * may render, and an empty week reads as an empty week.
 *
 * F142: "Critical Patients" counted ROWS, so one critical patient booked on
 * three days read as three. The day strip was six fixed 240px columns in a
 * sideways scroller whose scrollbar never showed; it now wraps.
 *
 * DEMO_READINESS §13.7 / §13.8 still hold: the TODAY badge is a non-shrinking
 * sibling of the truncating day label, and truncated names and reasons carry
 * title attributes so the full text is recoverable.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi, afterEach } from 'vitest'
import AppointmentsPanel from './AppointmentsPanel'
import { formatClinicalDate, localDateKey } from '../../lib/datetime'

// The signed-in role. Booking is offered only to a role holding patient.write.
const session = vi.hoisted(() => ({ role: 'doctor' }))

vi.mock('../../api/client', () => ({
  buildHeaders: () => ({}),
  getUser: () => ({ id: 'U1', name: 'Test User', role: session.role }),
}))

const LONG_NAME = 'Lakshmi Venkatasubramanian'
const LONG_REASON = 'Titrate medication and review imaging results with family'

const appt = (date: string, overrides: Partial<Record<string, unknown>> = {}) => ({
  id: `AP-${Math.random().toString(36).slice(2)}`,
  date,
  time: '09:30',
  patient_id: 'PAT-A73E5E98',
  patient_name: LONG_NAME,
  age: 61,
  sex: 'F',
  ward: 'W1',
  bed: '3',
  diagnosis: 'CAP',
  type: 'Clinical Review',
  reason: LONG_REASON,
  origin: 'staff',
  department: 'Medicine',
  acuity: 'stable',
  source: 'booked',
  ...overrides,
})

function serve(rows: unknown[]) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => rows })))
  return render(<AppointmentsPanel />)
}

function renderBoard() {
  const today = localDateKey()
  return serve([appt(today), appt(today, { time: '11:00', patient_id: 'PAT-B' })])
}

function tileValue(label: string): string {
  const card = screen.getByText(label, { selector: '.forecast-metric-label' })
    .closest('.forecast-metric-card') as HTMLElement
  return (card.querySelector('.metric-mono') as HTMLElement).textContent ?? ''
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AppointmentsPanel shows bookings only (F013)', () => {
  it('never renders a synthesised row, and says so when nothing is booked', async () => {
    const today = localDateKey()
    serve([appt(today, { source: 'predicted', patient_name: 'Invented Visit' })])
    expect(await screen.findByText('No appointments booked for the next 7 days.')).toBeInTheDocument()
    expect(screen.queryByText('Invented Visit')).toBeNull()
    expect(tileValue('Next 7 days')).toBe('0')
  })

  it('labels a patient with no risk score as not scored, never stable', async () => {
    serve([appt(localDateKey(), { acuity: null })])
    expect(await screen.findByText('Not scored')).toBeInTheDocument()
    expect(screen.queryByText('Stable')).toBeNull()
  })
})

describe('AppointmentsPanel tiles (F142)', () => {
  it('counts critical PATIENTS, not rows', async () => {
    const today = localDateKey()
    const d = new Date()
    d.setDate(d.getDate() + 1)
    const tomorrow = localDateKey(d)
    serve([
      appt(today, { patient_id: 'PAT-X', acuity: 'critical' }),
      appt(tomorrow, { patient_id: 'PAT-X', acuity: 'critical' }),
      appt(tomorrow, { patient_id: 'PAT-Y', acuity: 'critical', time: '10:00' }),
      appt(tomorrow, { patient_id: 'PAT-Z', acuity: 'stable', time: '11:00' }),
    ])
    await screen.findByText('Critical patients')
    expect(tileValue('Critical patients')).toBe('2')
    expect(tileValue('Today')).toBe('1')
    expect(tileValue('Next 7 days')).toBe('4')
    // The day count sits under the week total, not under Today.
    expect(screen.getByText('2 days with bookings')).toBeInTheDocument()
  })

  it('counts patient requests separately so their times get confirmed', async () => {
    serve([
      appt(localDateKey(), { origin: 'patient', type: 'Patient request: confirm time' }),
      appt(localDateKey(), { patient_id: 'PAT-B', time: '11:00' }),
    ])
    await screen.findByText('Patient requests')
    expect(tileValue('Patient requests')).toBe('1')
  })

  it('heads each day with its weekday, not a bare day-of-month number', async () => {
    renderBoard()
    const weekday = new Date().toLocaleDateString('en-GB', { weekday: 'short' })
    expect(await screen.findByText(weekday)).toBeInTheDocument()
  })
})

describe('AppointmentsPanel layout (§13.7, §13.8, F142)', () => {
  it('keeps the TODAY badge outside the ellipsized day label', async () => {
    renderBoard()
    const badge = await screen.findByText('Today', { selector: 'span:not(.forecast-metric-label)' })
    // Inside the truncating element the badge is the first text to be clipped
    // ("TOD…"); it must be a sibling that cannot shrink.
    const truncating = badge.closest('[style*="ellipsis"]')
    expect(truncating).toBeNull()
    expect(badge.style.flexShrink).toBe('0')
    const label = screen.getByText(formatClinicalDate(new Date()))
    expect(label.contains(badge)).toBe(false)
    expect(label.parentElement).toBe(badge.parentElement)
  })

  it('wraps day columns at a readable min width instead of scrolling sideways', async () => {
    renderBoard()
    const name = (await screen.findAllByText(LONG_NAME))[0]
    let el: HTMLElement | null = name.parentElement
    let dayGrid: HTMLElement | null = null
    while (el) {
      if (el.style.gridTemplateColumns.includes('repeat(')) dayGrid = el
      el = el.parentElement
    }
    // The outermost repeat() grid above a name is the day strip, not the KPI row.
    expect(dayGrid).toBeTruthy()
    expect(dayGrid!.style.gridTemplateColumns).toBe('repeat(auto-fill, minmax(240px, 1fr))')
    expect((dayGrid!.parentElement as HTMLElement).style.overflowX).not.toBe('auto')
  })

  it('makes truncated names and reasons recoverable via title attributes', async () => {
    renderBoard()
    const names = await screen.findAllByText(LONG_NAME)
    for (const n of names) expect(n).toHaveAttribute('title', LONG_NAME)
    for (const r of screen.getAllByText(LONG_REASON)) {
      expect(r).toHaveAttribute('title', LONG_REASON)
    }
  })
})

/* 2026-10-06: the badge is the NEWS2 band of the latest vitals WITH the
   number; a tile counts the post-discharge checks due this week; and staff can
   book from the panel (the POST had no caller in the app). */
describe('AppointmentsPanel score, tile and booking form', () => {
  it('prints the band word with the NEWS2 beside it', async () => {
    serve([
      appt(localDateKey(), { acuity: 'critical', news2: 9, news2_complete: true }),
      appt(localDateKey(), { patient_id: 'PAT-B', time: '11:00', acuity: 'elevated', news2: 5, news2_complete: false }),
    ])
    expect(await screen.findByText('Critical 9')).toBeInTheDocument()
    expect(screen.getByText('Elevated ≥5')).toBeInTheDocument()
  })

  it('counts the post-discharge checks due in the next 7 days', async () => {
    serve([
      appt(localDateKey(), { type_key: 'post_discharge_check', type_label: 'Post-discharge check' }),
      appt(localDateKey(), { patient_id: 'PAT-B', time: '11:00', type_key: 'follow_up', type_label: 'Follow-up' }),
    ])
    await screen.findByText('Post-discharge checks')
    expect(tileValue('Post-discharge checks')).toBe('1')
    expect(screen.getByText('Due in the next 7 days')).toBeInTheDocument()
    expect(screen.getByText('Post-discharge check')).toBeInTheDocument()
  })

  it('opens a booking form and posts the chosen type to the booking route', async () => {
    const { fireEvent, waitFor } = await import('@testing-library/react')
    const posted: { url: string; body: Record<string, unknown> }[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        posted.push({ url, body: JSON.parse(String(init.body)) })
        return { ok: true, json: async () => ({ id: 'NEW' }) }
      }
      if (url.startsWith('/api/vigil/patients')) {
        return { ok: true, json: async () => [{ id: 'PAT-W', name: 'Ward Patient', bed: 'GM-4' }] }
      }
      if (url.startsWith('/api/vigil/discharged')) {
        return { ok: true, json: async () => [
          { id: 'PAT-D', name: 'Home Yesterday', discharged_at: new Date(Date.now() - 86_400_000).toISOString() },
          { id: 'PAT-OLD', name: 'Home Long Ago', discharged_at: '2020-01-01 10:00:00' },
        ] }
      }
      return { ok: true, json: async () => [] }
    }))
    render(<AppointmentsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Book appointment' }))
    const form = await screen.findByRole('form', { name: 'Book an appointment' })
    await screen.findByRole('option', { name: 'Ward Patient, bed GM-4' })
    expect(screen.getByRole('option', { name: 'Home Yesterday' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Home Long Ago' })).toBeNull()
    for (const label of ['Follow-up', 'Medication review', 'Post-discharge check', 'Other']) {
      expect(screen.getByRole('option', { name: label })).toBeInTheDocument()
    }
    fireEvent.change(screen.getByLabelText('Patient'), { target: { value: 'PAT-D' } })
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'post_discharge_check' } })
    fireEvent.change(screen.getByLabelText('Time'), { target: { value: '23:59' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Wound check' } })
    fireEvent.submit(form)
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0].url).toBe('/api/appointments')
    expect(posted[0].body).toMatchObject({
      patient_id: 'PAT-D', appointment_type: 'post_discharge_check',
      scheduled_date: localDateKey(), scheduled_time: '23:59', reason: 'Wound check',
    })
  })
})

/* A nurse or an administrator can read the list but holds no patient.write, so
   the server refuses the booking. The button is not offered to them. */
describe('AppointmentsPanel booking is offered only to a role that can book', () => {
  afterEach(() => { session.role = 'doctor' })

  it('shows no Book appointment button to a nurse', async () => {
    session.role = 'nurse'
    serve([appt(localDateKey())])
    await screen.findByText('Post-discharge checks')
    expect(screen.queryByRole('button', { name: 'Book appointment' })).toBeNull()
  })

  it('shows it to a doctor', async () => {
    session.role = 'doctor'
    serve([appt(localDateKey())])
    expect(await screen.findByRole('button', { name: 'Book appointment' })).toBeInTheDocument()
  })
})

/* With scoring switched off (ORB_SCORING_ENABLED, the Orb Ward Record) the
   server sends no band. No badge and no critical count are drawn, so nothing
   reads "Not scored" about a patient whose observations are on file. */
describe('AppointmentsPanel with scoring switched off', () => {
  afterEach(async () => {
    const { resetClinicalSwitches } = await import('../../shellConfig')
    resetClinicalSwitches()
  })

  it('draws no band badge and no critical patients tile', async () => {
    const { act } = await import('@testing-library/react')
    const { setClinicalSwitches } = await import('../../shellConfig')
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    serve([appt(localDateKey(), { acuity: null, news2: null })])
    await screen.findByText('Post-discharge checks')
    expect(screen.queryByText('Not scored')).toBeNull()
    expect(screen.queryByText('Critical patients')).toBeNull()
    expect(document.querySelector('.appt-band')).toBeNull()
  })

  it('with scoring on, the badge and the tile are drawn', async () => {
    serve([appt(localDateKey(), { acuity: 'critical', news2: 9, news2_complete: true })])
    expect(await screen.findByText('Critical 9')).toBeInTheDocument()
    expect(screen.getByText('Critical patients')).toBeInTheDocument()
  })
})
