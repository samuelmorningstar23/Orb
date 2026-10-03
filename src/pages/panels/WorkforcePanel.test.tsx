/**
 * WorkforcePanel — the four things this screen must never get wrong.
 *
 * 1. THE STATUTORY BOUNDARY IS ON SCREEN BEFORE THE NETWORK ANSWERS. An HR
 *    clerk who believes Orb computed the PF and forwards the file unchecked has
 *    been failed by this screen. The sentence must therefore survive a total
 *    network failure — which is exactly when a panel usually renders nothing.
 *
 * 2. A DAY NOBODY RECORDED IS NOT AN ABSENCE. It has its own column, its own
 *    label ("Not recorded") and its own tone, everywhere it appears. Folding it
 *    into present or absent is the difference between a nurse being paid and
 *    not, and it is silent in both directions.
 *
 * 3. THE STATUTORY COLUMNS RENDER AS DELIBERATELY EMPTY. The preview shows the
 *    six PF/ESI/PT/TDS columns, blank, beside a row that says who computes them.
 *    A screen that hid the empty columns would let somebody assume they were
 *    filled in somewhere.
 *
 * 4. A DISABLED MODULE IS NOT AN EMPTY ROSTER. When a site runs its own HRMS
 *    the routes 404 with `module_disabled`. That must render as "not available
 *    at this site", never as a roster with nobody on it — an empty roster is a
 *    statement about the hospital's staffing.
 *
 * PROVEN ABLE TO FAIL (2026-08-12). Five defects were reintroduced into
 * WorkforcePanel.tsx one at a time, the suite watched go RED, and the file
 * restored:
 *
 *   1. the first-paint literal removed, so the boundary depends on /meta
 *      → both boundary tests
 *   2. STATUS_LABEL.unrecorded changed to 'Absent'
 *      → the roster label test
 *   3. the statutory columns filtered out of the preview table
 *      → the "shows them, empty" test
 *   4. the `moduleOff` early return removed
 *      → the switched-off test
 *   5. the muster's "Not recorded" column header renamed to "Absent"
 *      → the muster column test
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import WorkforcePanel from './WorkforcePanel'
import { WorkforceApiError } from '../../api/workforce'

vi.mock('../../api/workforce', async () => {
  const actual = await vi.importActual<typeof import('../../api/workforce')>(
    '../../api/workforce',
  )
  return {
    ...actual,
    getMeta: vi.fn(),
    getEmployees: vi.fn(),
    getRoster: vi.fn(),
    getRosterConflicts: vi.fn(),
    getAttendance: vi.fn(),
    getSummary: vi.fn(),
    previewPayrollExport: vi.fn(),
    getExportRuns: vi.fn(),
    getHolidays: vi.fn(),
    cancelRosterEntry: vi.fn(),
    saveEmployee: vi.fn(),
  }
})

const api = await import('../../api/workforce')

const STATUTORY = [
  'pf_employee_inr', 'pf_employer_inr', 'esi_employee_inr',
  'esi_employer_inr', 'professional_tax_inr', 'tds_inr',
]

const META = {
  shift_types: [
    { code: 'MORNING', name: 'Morning (07:00-14:00)', start_minute: 420,
      duration_minutes: 420, start_time: '07:00', end_time: '14:00',
      crosses_midnight: false, allowance_code: null, is_active: true },
    { code: 'NIGHT', name: 'Night (21:00-07:00)', start_minute: 1260,
      duration_minutes: 600, start_time: '21:00', end_time: '07:00',
      crosses_midnight: true, allowance_code: 'night' as const, is_active: true },
  ],
  leave_types: [{ code: 'CL', name: 'Casual leave', is_paid: true, is_active: true }],
  day_statuses: ['present', 'absent', 'leave', 'weekly_off', 'holiday'] as const,
  employment_types: ['permanent'],
  attendance_sources: ['manual'],
  policy: {
    overtime_daily_minutes: 480, late_grace_minutes: 10, max_span_minutes: 960,
    overtime_basis: 'Minutes worked beyond the daily threshold, per day.',
    statutory_boundary:
      'Orb records the roster and attendance. PF, ESI, Professional Tax and TDS '
      + 'are computed by the hospital’s payroll provider, not by Orb.',
  },
  min_rest_minutes: 480,
  payroll_export_columns: ['employee_code', 'days_present', 'days_unrecorded',
                           ...STATUTORY, 'statutory_computed_by'],
  statutory_columns: STATUTORY,
  statutory_computed_by: 'payroll_provider',
  boundary: {
    orb_owns: 'duty roster, attendance, overtime hours, shift counts',
    provider_owns: 'salary, PF, ESI, Professional Tax, TDS, payslips',
    message:
      'Orb records the roster and attendance. PF, ESI, Professional Tax and TDS '
      + 'are computed by the hospital’s payroll provider, not by Orb.',
  },
}

const EMPTY_BOARD = {
  date_from: '2026-06-01', date_to: '2026-06-07', department: null,
  days: [], shift_types: [],
}

function mockHappyPath() {
  vi.mocked(api.getMeta).mockResolvedValue(META as never)
  vi.mocked(api.getEmployees).mockResolvedValue({ employees: [] })
  vi.mocked(api.getRoster).mockResolvedValue(EMPTY_BOARD as never)
  vi.mocked(api.getRosterConflicts).mockResolvedValue({ conflicts: [], conflict_count: 0 })
  vi.mocked(api.getAttendance).mockResolvedValue({ attendance: [], count: 0 })
  vi.mocked(api.getHolidays).mockResolvedValue({ holidays: [] })
  vi.mocked(api.getExportRuns).mockResolvedValue({ runs: [] })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockHappyPath()
})

describe('the statutory boundary', () => {
  it('is on screen before any request resolves', () => {
    // Every call left pending: this is the first paint, and the moment the
    // sentence matters most.
    vi.mocked(api.getMeta).mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(api.getEmployees).mockReturnValue(new Promise(() => {}) as never)

    render(<WorkforcePanel />)

    const text = document.body.textContent ?? ''
    expect(text).toMatch(/does not compute pay/i)
    expect(text).toMatch(/PF, ESI, Professional Tax and TDS/i)
    expect(text).toMatch(/payroll provider/i)
  })

  it('survives a total network failure', async () => {
    vi.mocked(api.getMeta).mockRejectedValue(new Error('network down'))
    vi.mocked(api.getEmployees).mockRejectedValue(new Error('network down'))

    render(<WorkforcePanel />)

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/PF, ESI, Professional Tax and TDS/i)
    })
  })

  it('prefers the server’s own wording once /meta answers', async () => {
    render(<WorkforcePanel />)
    await waitFor(() => {
      expect(screen.getByText(/computed by the hospital’s payroll provider, not by Orb/i))
        .toBeTruthy()
    })
  })
})

describe('a day nobody recorded', () => {
  it('is labelled "Not recorded" on the roster, never "Absent"', async () => {
    vi.mocked(api.getRoster).mockResolvedValue({
      ...EMPTY_BOARD,
      days: [{
        date: '2026-06-01', is_holiday: false, weekday: 0,
        shifts: [{
          shift_code: 'MORNING', shift_name: 'Morning (07:00-14:00)',
          start_time: '07:00', end_time: '14:00',
          planned_count: 1, present_count: 0, absent_count: 0,
          leave_count: 0, unrecorded_count: 1,
          assignments: [{
            roster_id: 1, employee_code: 'E1', employee_name: 'Anita Rao',
            designation: 'Staff Nurse', department: 'Ward A', ward: null,
            status: 'planned', actual_status: 'unrecorded',
            actual_in: null, actual_out: null,
            worked_minutes: 0, overtime_minutes: 0, late_minutes: 0,
          }],
        }],
      }],
    } as never)

    render(<WorkforcePanel />)

    await waitFor(() => expect(screen.getByText('Anita Rao')).toBeTruthy())
    expect(screen.getByText('Not recorded')).toBeTruthy()
    expect(screen.queryByText('Absent')).toBeNull()
    // And the shift header says how many, so a gap is countable at a glance.
    expect(screen.getByText(/1 not recorded/)).toBeTruthy()
  })

  it('has its own column in the monthly muster, distinct from absences', async () => {
    vi.mocked(api.getSummary).mockResolvedValue({
      period_start: '2026-06-01', period_end: '2026-06-30', year: 2026, month: 6,
      department: null, employee_count: 1, incomplete_employee_count: 1,
      unrecorded_day_count: 3, identity_holds: true,
      totals: { days_present: 22, overtime_hours: '4.50' },
      employees: [{
        employee_code: 'E1', employee_name: 'Anita Rao', department: 'Ward A',
        designation: 'Staff Nurse', employment_type: 'permanent',
        payroll_bureau_ref: null, period_start: '2026-06-01', period_end: '2026-06-30',
        days_in_period: 30, days_working: 26, days_present: 22, days_absent: 0,
        days_leave_paid: 1, days_leave_unpaid: 0, days_leave_total: 1,
        days_weekly_off: 4, days_holiday: 0, days_unrecorded: 3,
        days_absent_or_unpaid_leave: 0, leave_by_type: { CL: 1 },
        attendance_complete: false, unrecorded_dates: [],
        worked_minutes: 10800, worked_hours: '180.00',
        overtime_minutes: 270, overtime_hours: '4.50',
        late_marks: 0, late_minutes: 0, night_shift_count: 5,
        evening_shift_count: 0, holiday_shift_count: 0, weekly_off_worked_count: 0,
        identity: { buckets_sum: 30, days_in_period: 30, holds: true, statement: '' },
      }],
    } as never)

    render(<WorkforcePanel />)
    fireEvent.click(screen.getByRole('tab', { name: 'Monthly muster' }))

    await waitFor(() => expect(screen.getByText('Anita Rao')).toBeTruthy())
    expect(screen.getByRole('columnheader', { name: 'Not recorded' })).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: 'Absent' })).toBeTruthy()

    const row = screen.getByText('Anita Rao').closest('tr')!
    const cells = within(row).getAllByRole('cell')
    // Person | Working | Present | Absent | Leave | Not recorded | OT | Nights
    expect(cells[1].textContent).toBe('26')
    expect(cells[2].textContent).toBe('22')
    expect(cells[3].textContent).toBe('0')     // zero absences…
    expect(cells[5].textContent).toBe('3')     // …and three unrecorded days
    // The tile makes the same point without the reader having to scan a table.
    expect(screen.getByText('Days not recorded')).toBeTruthy()
  })
})

describe('a month still in progress', () => {
  it('shows a future rostered line as Scheduled, not as a gap', async () => {
    vi.mocked(api.getRoster).mockResolvedValue({
      ...EMPTY_BOARD,
      days: [{
        date: '2026-10-20', is_holiday: false, weekday: 1,
        shifts: [{
          shift_code: 'MORNING', shift_name: 'Morning (07:00-14:00)',
          start_time: '07:00', end_time: '14:00',
          planned_count: 1, present_count: 0, absent_count: 0,
          leave_count: 0, unrecorded_count: 0, scheduled_count: 1,
          assignments: [{
            roster_id: 2, employee_code: 'E2', employee_name: 'Kavya Nair',
            designation: 'Staff Nurse', department: 'Ward A', ward: null,
            status: 'planned', actual_status: 'scheduled',
            actual_in: null, actual_out: null,
            worked_minutes: 0, overtime_minutes: 0, late_minutes: 0,
          }],
        }],
      }],
    } as never)

    render(<WorkforcePanel />)

    await waitFor(() => expect(screen.getByText('Kavya Nair')).toBeTruthy())
    const badge = screen.getByText('Scheduled')
    expect(badge.className).toContain('wf-badge--neutral')
    expect(screen.queryByText('Not recorded')).toBeNull()
    expect(screen.queryByText(/not recorded/)).toBeNull()
    expect(screen.getByText(/1 scheduled/)).toBeTruthy()
  })

  it('counts the muster to today and reports the rest as still to come', async () => {
    vi.mocked(api.getSummary).mockResolvedValue({
      period_start: '2026-10-01', period_end: '2026-10-31', year: 2026, month: 10,
      department: null, counted_through: '2026-10-03', period_closed: false,
      employee_count: 1, incomplete_employee_count: 1,
      unrecorded_day_count: 0, still_to_come_day_count: 28, identity_holds: true,
      totals: { days_present: 3, overtime_hours: '0.00' },
      employees: [],
    } as never)

    render(<WorkforcePanel />)
    fireEvent.click(screen.getByRole('tab', { name: 'Monthly muster' }))

    const notice = await screen.findByTestId('wf-muster-open-month')
    expect(notice.textContent).toContain('Counted up to 3 Oct 2026')
    expect(screen.getByText('Days still to come')).toBeTruthy()
    const tile = screen.getByText('Days not recorded').parentElement!
    expect(tile.textContent).toContain('0')
  })
})

describe('the payroll export preview', () => {
  const PREVIEW = {
    columns: META.payroll_export_columns,
    rows: [{
      employee_code: 'E1', days_present: '22', days_unrecorded: '3',
      pf_employee_inr: '', pf_employer_inr: '', esi_employee_inr: '',
      esi_employer_inr: '', professional_tax_inr: '', tds_inr: '',
      statutory_computed_by: 'payroll_provider',
    }],
    period_start: '2026-06-01', period_end: '2026-06-30', year: 2026, month: 6,
    department: null, employee_count: 1, incomplete_employee_count: 1,
    file_name: 'orb-payroll-2026-06.csv',
    warnings: ['1 employee(s) have 3 day(s) with no attendance record.'],
    statutory_note: 'PF, ESI, Professional Tax and TDS columns are intentionally empty.',
  }

  it('shows the statutory columns, empty, rather than hiding them', async () => {
    vi.mocked(api.previewPayrollExport).mockResolvedValue(PREVIEW as never)

    render(<WorkforcePanel />)
    fireEvent.click(screen.getByRole('tab', { name: 'Payroll export' }))

    await waitFor(() => expect(screen.getByText('orb-payroll-2026-06.csv')).toBeTruthy())

    for (const column of STATUTORY) {
      // Present as a header — a hidden empty column lets somebody assume it was
      // filled in somewhere else.
      expect(screen.getByRole('columnheader', { name: column })).toBeTruthy()
    }
    // And the row carries the stamp naming who does compute them.
    expect(screen.getAllByText('payroll_provider').length).toBeGreaterThan(0)
  })

  it('names what Orb provides and what the provider computes', async () => {
    vi.mocked(api.previewPayrollExport).mockResolvedValue(PREVIEW as never)

    render(<WorkforcePanel />)
    fireEvent.click(screen.getByRole('tab', { name: 'Payroll export' }))

    await waitFor(() => expect(screen.getByText('Orb provides')).toBeTruthy())
    expect(screen.getByText('The payroll provider computes')).toBeTruthy()
    // Scoped to the "provider computes" list: the same words also appear in the
    // boundary notice at the top of the panel, and an unscoped query that
    // matches either would keep passing if this list were deleted.
    const providerList = screen.getByText('The payroll provider computes')
      .parentElement!.querySelector('.wf-statutory__list')!
    const items = within(providerList as HTMLElement).getAllByRole('listitem')
      .map(li => li.textContent)
    expect(items).toContain('Provident Fund (employee and employer)')
    expect(items).toContain('ESI (employee and employer)')
    expect(items).toContain('Professional Tax and TDS')
  })

  it('surfaces the unrecorded-days warning instead of exporting quietly', async () => {
    vi.mocked(api.previewPayrollExport).mockResolvedValue(PREVIEW as never)

    render(<WorkforcePanel />)
    fireEvent.click(screen.getByRole('tab', { name: 'Payroll export' }))

    await waitFor(() => {
      expect(screen.getByText(/no attendance record/i)).toBeTruthy()
    })
    expect(screen.getByText('With unrecorded days')).toBeTruthy()
    // The button is still enabled: a hospital that cannot run payroll at all
    // goes back to the paper muster, which is worse than a flagged file.
    const button = screen.getByRole('button', { name: /Generate & download/ })
    expect((button as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('a switched-off module', () => {
  it('says so rather than rendering an empty roster', async () => {
    vi.mocked(api.getMeta).mockRejectedValue(
      new WorkforceApiError('The workforce module is switched off for this '
        + 'deployment (ORB_WORKFORCE=0).', 404, 'module_disabled'),
    )
    vi.mocked(api.getEmployees).mockRejectedValue(
      new WorkforceApiError('off', 404, 'module_disabled'),
    )

    render(<WorkforcePanel />)

    await waitFor(() => expect(screen.getByText(/Not available at this site/i)).toBeTruthy())
    expect(screen.getByText(/ORB_WORKFORCE=0/)).toBeTruthy()
    // NOT an empty roster — that would be a claim about the hospital's staffing.
    expect(screen.queryByText(/Nobody is rostered this week/i)).toBeNull()
    expect(screen.queryByRole('tab', { name: 'Duty roster' })).toBeNull()
  })
})

describe('no money anywhere', () => {
  it('renders no rupee figure on the export screen', async () => {
    vi.mocked(api.previewPayrollExport).mockResolvedValue({
      columns: META.payroll_export_columns, rows: [],
      period_start: '2026-06-01', period_end: '2026-06-30', year: 2026, month: 6,
      department: null, employee_count: 0, incomplete_employee_count: 0,
      file_name: 'orb-payroll-2026-06.csv', warnings: [],
      statutory_note: 'intentionally empty',
    } as never)

    render(<WorkforcePanel />)
    fireEvent.click(screen.getByRole('tab', { name: 'Payroll export' }))

    await waitFor(() => expect(screen.getByText('Orb provides')).toBeTruthy())
    // A rupee glyph on this screen would be a promise the module refuses to
    // make. The only currency word permitted is inside a column NAME (…_inr).
    const rendered = document.body.textContent ?? ''
    expect(rendered).not.toMatch(/₹/)
  })
})


/* ═══════════════════════════════════════════════════════════════════════════
   Taking someone off a shift is asked, and a refusal is shown
   Remove cancelled the roster line on one tap, with no catch: a refusal from
   the server became an unhandled rejection and the screen said nothing.
   ═══════════════════════════════════════════════════════════════════════════ */

function oneLineBoard() {
  return {
    ...EMPTY_BOARD,
    days: [{
      date: '2026-06-01', is_holiday: false, weekday: 0,
      shifts: [{
        shift_code: 'MORNING', shift_name: 'Morning (07:00-14:00)',
        start_time: '07:00', end_time: '14:00',
        planned_count: 1, present_count: 0, absent_count: 0,
        leave_count: 0, unrecorded_count: 1,
        assignments: [{
          roster_id: 7, employee_code: 'E7', employee_name: 'Anita Rao',
          designation: 'Staff Nurse', department: 'Ward A', ward: 'GM-1',
          status: 'planned', actual_status: 'unrecorded',
          actual_in: null, actual_out: null,
          worked_minutes: 0, overtime_minutes: 0, late_minutes: 0,
        }],
      }],
    }],
  } as never
}

describe('removing a roster line', () => {
  it('asks first, naming the person, the shift and the day', async () => {
    vi.mocked(api.getRoster).mockResolvedValue(oneLineBoard())
    vi.mocked(api.cancelRosterEntry).mockResolvedValue({} as never)
    render(<WorkforcePanel />)
    fireEvent.click(await screen.findByRole('button', { name: /Remove Anita Rao/ }))
    expect(api.cancelRosterEntry).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: 'Remove from the roster' })
    expect(dialog.textContent).toContain('Anita Rao')
    expect(dialog.textContent).toContain('Staff Nurse, GM-1')
    expect(dialog.textContent).toContain('Morning (07:00-14:00)')
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Remove from roster' }))
    })
    expect(api.cancelRosterEntry).toHaveBeenCalledWith(7)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('shows a refusal in the dialog instead of losing it', async () => {
    vi.mocked(api.getRoster).mockResolvedValue(oneLineBoard())
    vi.mocked(api.cancelRosterEntry).mockRejectedValue(
      new WorkforceApiError('Attendance is already recorded for this shift.', 409, 'has_attendance'))
    render(<WorkforcePanel />)
    fireEvent.click(await screen.findByRole('button', { name: /Remove Anita Rao/ }))
    const dialog = screen.getByRole('dialog', { name: 'Remove from the roster' })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Remove from roster' }))
    })
    expect(within(dialog).getByRole('alert').textContent)
      .toContain('Attendance is already recorded for this shift.')
  })
})
