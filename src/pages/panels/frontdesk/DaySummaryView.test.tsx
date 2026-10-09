/**
 * Front Desk, Day summary (opd-18).
 *
 *  1. The day's counts as the server sends them: a wait nobody was measured
 *     for reads "Not measured", never 0; no-shows say when they are not set up.
 *  2. Outpatients switched off reads as words, and only the registration
 *     feature going away drops the desk's views.
 *  3. Choosing a day asks the server for that day.
 *  4. Export CSV writes the same counts to a file on this computer, with any
 *     formula-looking cell defused, and names no patient.
 *  5. The view installs itself as the desk's 'Day summary' tab for
 *     registration.read holders.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
}))

import DaySummaryView from './DaySummaryView'
import { csvCell, dailyMisCsv, minutesText, type DailyMis } from '../../../api/frontOfficeMis'
import { discoverFrontDeskViews } from './viewRegistry'

const wait = (median: number | null, longest: number | null, measured = 0) =>
  ({ measured, median_minutes: median, longest_minutes: longest })

const MIS: DailyMis = {
  date: '2026-09-15',
  department: 'Cardiology',
  generated_at: '2026-09-15T10:00:00+00:00',
  registrations: { new: 2, returning: 6, not_known: 1, patients: 9 },
  visits: {
    total: 9,
    by_status: { waiting: 2, in_consultation: 1, completed: 3, cancelled: 3 },
    by_type: [
      { code: 'new', label: 'New', count: 7 }, { code: 'follow_up', label: 'Follow-up', count: 1 },
      { code: 'review', label: 'Review', count: 1 }, { code: 'procedure', label: 'Procedure', count: 0 },
    ],
  },
  waits: { ...wait(15, 30, 4), still_waiting: 2 },
  left_without_being_seen: 1,
  cancellations: [
    { code: 'left_without_being_seen', label: 'Left without being seen', count: 1 },
    { code: 'no_show', label: 'Did not arrive', count: 1 },
    { code: 'not_coded', label: 'No reason code', count: 1 },
  ],
  no_shows: { available: true, recorded: 1, computed: 1, total: 2 },
  conversions: 1,
  clinics: [{
    department: 'Cardiology', total: 9, waiting: 2, in_consultation: 1, completed: 3,
    cancelled: 3, left_without_being_seen: 1, admitted: 1, wait: wait(15, 30, 4),
    doctors: [
      { doctor_id: 'DOC-1', doctor_name: 'Dr Iyer', total: 8, waiting: 1, in_consultation: 1,
        completed: 3, cancelled: 3, left_without_being_seen: 1, admitted: 1, wait: wait(15, 30, 4) },
      { doctor_id: null, doctor_name: 'Unassigned', total: 1, waiting: 1, in_consultation: 0,
        completed: 0, cancelled: 0, left_without_being_seen: 0, admitted: 0, wait: wait(null, null) },
    ],
  }],
}

const props = (onFeatureOff = vi.fn()) => ({
  scopedDepartment: 'Cardiology', openView: vi.fn(), onFeatureOff,
})

function featureOff(feature: string) {
  return Object.assign(new Error('switched off'), {
    status: 404, detail: { error: 'feature_disabled', feature },
  })
}

beforeEach(() => {
  apiFetch.mockReset()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('the day summary', () => {
  it('shows the counts, and a wait nobody was measured for as words', async () => {
    apiFetch.mockResolvedValue(MIS)
    render(<DaySummaryView {...props()} />)
    const tiles = await screen.findByLabelText('The day in numbers')
    const tile = (label: string) => within(tiles).getByText(label).parentElement as HTMLElement
    expect(within(tile('New registrations')).getByText('2')).toBeInTheDocument()
    expect(within(tile('Returning patients')).getByText('6')).toBeInTheDocument()
    expect(within(tile('Median wait to be called')).getByText('15 min')).toBeInTheDocument()
    expect(within(tile('Longest wait to be called')).getByText('30 min')).toBeInTheDocument()
    expect(within(tile('Left without being seen')).getByText('1')).toBeInTheDocument()
    expect(within(tile('No-shows')).getByText('2')).toBeInTheDocument()
    expect(within(tile('Admitted from outpatients')).getByText('1')).toBeInTheDocument()
    expect(screen.getByText(/1 patient has no registration time on record/)).toBeInTheDocument()

    const unassigned = screen.getByText('Unassigned').closest('tr') as HTMLElement
    expect(within(unassigned).getAllByText('Not measured')).toHaveLength(2)
    expect(screen.queryByText('0 min')).toBeNull()
    expect(apiFetch).toHaveBeenCalledWith('/frontdesk/mis/daily', undefined)
  })

  it('says when no-shows are not set up', async () => {
    apiFetch.mockResolvedValue({ ...MIS, no_shows: { available: false, recorded: 0, computed: 0, total: 0 } })
    render(<DaySummaryView {...props()} />)
    const tiles = await screen.findByLabelText('The day in numbers')
    expect(within(tiles).getByText('Not available')).toBeInTheDocument()
    expect(screen.getByText(/not set up on this site yet/)).toBeInTheDocument()
  })

  it('says in words when outpatients are switched off, and keeps the desk', async () => {
    const onFeatureOff = vi.fn()
    apiFetch.mockRejectedValue(featureOff('opd'))
    render(<DaySummaryView {...props(onFeatureOff)} />)
    expect(await screen.findByText(/needs Outpatients switched on/)).toBeInTheDocument()
    expect(onFeatureOff).not.toHaveBeenCalled()
  })

  it('drops the desk views when registration is switched off', async () => {
    const onFeatureOff = vi.fn()
    apiFetch.mockRejectedValue(featureOff('registration'))
    render(<DaySummaryView {...props(onFeatureOff)} />)
    await waitFor(() => expect(onFeatureOff).toHaveBeenCalled())
  })

  it('asks the server for the day chosen', async () => {
    apiFetch.mockResolvedValue(MIS)
    render(<DaySummaryView {...props()} />)
    await screen.findByLabelText('The day in numbers')
    fireEvent.change(screen.getByLabelText('Day'), { target: { value: '2026-09-01' } })
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/frontdesk/mis/daily?date=2026-09-01', undefined))
  })

  it('exports the counts as a CSV file on this computer', async () => {
    apiFetch.mockResolvedValue(MIS)
    const made: Blob[] = []
    const create = vi.fn((blob: Blob) => { made.push(blob); return 'blob:day-summary' })
    const revoke = vi.fn()
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<DaySummaryView {...props()} />)
    await screen.findByLabelText('The day in numbers')
    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(create).toHaveBeenCalledTimes(1)
    expect(made[0].type).toBe('text/csv;charset=utf-8')
    expect(click).toHaveBeenCalledTimes(1)
    expect(revoke).toHaveBeenCalledWith('blob:day-summary')
  })
})

describe('the CSV', () => {
  it('carries the counts and one row per doctor', () => {
    const csv = dailyMisCsv(MIS)
    expect(csv).toContain('Clinic,Cardiology')
    expect(csv).toContain('New registrations,2')
    expect(csv).toContain('Returning patients,6')
    expect(csv).toContain('Left without being seen,1')
    expect(csv).toContain('No-shows,2')
    expect(csv).toContain('Median wait to be called (minutes),15')
    expect(csv).toContain('Cardiology,Dr Iyer,8,3,3,1,1,15,30')
    expect(csv).toContain('Cardiology,Unassigned,1,0,0,0,0,Not measured,Not measured')
    expect(csv.endsWith('\r\n')).toBe(true)
  })

  it('defuses a cell a spreadsheet would run as a formula', () => {
    expect(csvCell('=HYPERLINK("x")')).toBe(`"'=HYPERLINK(""x"")"`)
    expect(csvCell('+91')).toBe("'+91")
    expect(csvCell('-1')).toBe("'-1")
    expect(csvCell('@me')).toBe("'@me")
    expect(csvCell('Dr Rao, senior')).toBe('"Dr Rao, senior"')
    expect(csvCell(-1)).toBe('-1')
    expect(csvCell(null)).toBe('')
    const named = dailyMisCsv({
      ...MIS,
      clinics: [{ ...MIS.clinics[0], doctors: [{ ...MIS.clinics[0].doctors[0], doctor_name: '=cmd' }] }],
    })
    expect(named).toContain("Cardiology,'=cmd,")
  })

  it('writes minutes as words when not measured', () => {
    expect(minutesText(null)).toBe('Not measured')
    expect(minutesText(37.5)).toBe('37.5 min')
    expect(minutesText(0)).toBe('0 min')
  })
})

describe('the desk tab', () => {
  it('installs as Day summary for registration.read holders', () => {
    const installed = discoverFrontDeskViews(import.meta.glob('./*View.tsx', { eager: true }))
    const view = installed.find(v => v.id === 'day-summary')
    expect(view?.label).toBe('Day summary')
    expect(view?.permission).toBe('registration.read')
    expect(view?.order).toBe(40)
  })
})
