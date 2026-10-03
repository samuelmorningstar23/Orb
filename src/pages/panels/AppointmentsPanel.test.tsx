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

vi.mock('../../api/client', () => ({
  buildHeaders: () => ({}),
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
