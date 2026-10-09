/**
 * The Turnaround times view (lis-18). What it must never get wrong:
 *
 *   · A group with no target says 'No target', never 0%.
 *   · The overdue list names the test, the sample and how far past target.
 *   · The figures say when simulated results were left out.
 *   · The export downloads the server's CSV.
 *   · A failed load is never an empty board; a switched-off module goes to
 *     the panel.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../../api/lab'
import { minutesText, type TatDashboard, type TatFigures } from '../../../api/lab/tat'
import TatView, * as tatModule from './TatView'
import { discoverViews } from './viewRegistry'

vi.mock('../../../api/lab/tat', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/tat')>('../../../api/lab/tat')
  return { ...actual, getLabTat: vi.fn(), exportLabTat: vi.fn() }
})

const api = vi.mocked(await import('../../../api/lab/tat'))

function figures(over: Partial<TatFigures> = {}): TatFigures {
  return {
    count: 4,
    intervals: {
      order_to_collection: { n: 4, median: 20, p90: 30 },
      collection_to_receipt: { n: 4, median: 15, p90: 25 },
      receipt_to_report: { n: 4, median: 45, p90: 95 },
      order_to_report: { n: 4, median: 80, p90: 140 },
    },
    target: { with_target: 3, within: 2, no_target: 1, unreadable: 0, percent: 66.7,
      state: 'reported', text: '66.7%' },
    out_of_order: 0,
    ...over,
  }
}

function board(over: Partial<TatDashboard> = {}): TatDashboard {
  return {
    period: { first_day: '2026-10-03', last_day: '2026-10-09', days: 7, start: '', end: '',
      timezone: 'IST' },
    intervals: [],
    target_interval: 'receipt_to_report',
    overall: figures(),
    by_section_priority: [
      { ...figures(), section_id: 'biochemistry', section_name: 'Biochemistry', priority: 'routine',
        priority_text: 'Routine' },
      { ...figures({ count: 2, target: { with_target: 0, within: 0, no_target: 2, unreadable: 0,
        percent: null, state: 'no_target', text: 'No target' } }),
      section_id: 'haematology', section_name: 'Haematology', priority: 'stat', priority_text: 'STAT' },
    ],
    by_day: [{ ...figures(), day: '2026-10-09' }],
    simulated_excluded: 2,
    critical: { raised: 3, acknowledged: 2, by_phone_log: 1, by_ward_read_back: 1, open: 1,
      oldest_open_minutes: 125, median_minutes: 17.5, p90_minutes: 25, state: 'reported' },
    overdue: { items: [{
      item_id: 9, specimen_id: '261009000127', order_id: 'O-9', priority: 'stat',
      priority_text: 'STAT', status: 'received',
      test: { id: 'LOINC:2823-3', name: 'Potassium', short_name: 'K' },
      section_name: 'Biochemistry',
      patient: { id: 'P1', name: 'Asha Menon', mrn: 'MRN-0042', ward: 'GM-1', bed: '4A' },
      received_at: '2026-10-09T03:50:00+00:00', target_minutes: 30, waiting_minutes: 75,
      over_by_minutes: 45,
    }], total: 1, in_lab: 5, in_lab_without_target: 2 },
    exceptions: { open: 2, by_kind: [{ kind: 'intake_failed', label: 'Not taken into the laboratory yet',
      count: 2 }] },
    limits: { max_period_days: 92, overdue_cap: 200 },
    scope: { department: null, hospital_wide: true },
    ...over,
  }
}

const props = { meta: {}, state: 'on' as const, openView: vi.fn(), onModuleOff: vi.fn() }

beforeEach(() => {
  api.getLabTat.mockResolvedValue(board())
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('TatView', () => {
  it('declares itself to the panel as the turnaround tab', () => {
    const [entry] = discoverViews({ './lab/TatView.tsx': tatModule })
    expect(entry).toMatchObject({ id: 'turnaround', label: 'Turnaround times', order: 80,
      permission: 'lab.read', whenDraining: 'show' })
  })

  it('shows the figures, and a group with no target says so instead of 0%', async () => {
    render(<TatView {...props} />)
    const groups = await screen.findByRole('table', { name: 'By section and priority' })
    const haem = within(groups).getByText('Haematology, STAT').closest('tr') as HTMLElement
    expect(within(haem).getByText('No target')).toBeTruthy()
    expect(within(haem).queryByText(/0(\.0)?%/)).toBeNull()
    const bio = within(groups).getByText('Biochemistry, Routine').closest('tr') as HTMLElement
    expect(within(bio).getByText('66.7% (2 of 3)')).toBeTruthy()
    expect(within(bio).getByText('45 min')).toBeTruthy()
    expect(within(bio).getByText('1 h 35 min')).toBeTruthy()
    expect(screen.getByText('2 of 3, median 17.5 min')).toBeTruthy()
  })

  it('lists the tests past their target, STAT first', async () => {
    render(<TatView {...props} />)
    const overdue = await screen.findByRole('table', { name: 'Past their target now' })
    const first = within(overdue).getAllByRole('row')[1]
    expect(within(first).getByText('STAT')).toBeTruthy()
    expect(within(first).getByText('Potassium')).toBeTruthy()
    expect(within(first).getByText('261009 00012 7')).toBeTruthy()
    expect(within(first).getByText('45 min')).toBeTruthy()
  })

  it('says when simulated results were left out and criticals are still open', async () => {
    render(<TatView {...props} />)
    expect(await screen.findByText('2 simulated results left out of these figures.')).toBeTruthy()
    expect(screen.getByText(/1 critical result not yet acknowledged, the oldest raised 2 h 5 min ago/))
      .toBeTruthy()
  })

  it('asks for the period chosen', async () => {
    render(<TatView {...props} />)
    await screen.findByRole('table', { name: 'By day' })
    fireEvent.change(screen.getByLabelText('First day'), { target: { value: '2026-09-01' } })
    fireEvent.change(screen.getByLabelText('Last day'), { target: { value: '2026-09-30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Show' }))
    await waitFor(() => expect(api.getLabTat).toHaveBeenLastCalledWith(
      { from: '2026-09-01', to: '2026-09-30' }))
  })

  it('downloads the server CSV', async () => {
    api.exportLabTat.mockResolvedValue({ filename: 'lab-turnaround.csv', content_type: 'text/csv',
      rows: 4, csv: 'Hospital day,Accession\r\n' })
    const create = vi.fn(() => 'blob:tat')
    const revoke = vi.fn()
    URL.createObjectURL = create
    URL.revokeObjectURL = revoke
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<TatView {...props} />)
    await screen.findByRole('table', { name: 'By day' })
    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(await screen.findByText('Exported 4 tests.')).toBeTruthy()
    expect(create).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
    expect(revoke).toHaveBeenCalledWith('blob:tat')
    click.mockRestore()
  })

  it('never shows a failed load as an empty board', async () => {
    api.getLabTat.mockRejectedValue(new LabApiError('The audit trail is not available', 503,
      'audit_unavailable'))
    render(<TatView {...props} />)
    expect((await screen.findByRole('alert')).textContent).toContain('The audit trail is not available')
    expect(screen.queryByText('Tests reported')).toBeNull()
  })

  it('hands a switched-off module to the panel', async () => {
    api.getLabTat.mockRejectedValue(new LabApiError('off', 404, 'module_disabled'))
    const onModuleOff = vi.fn()
    render(<TatView {...props} onModuleOff={onModuleOff} />)
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })

  it('writes minutes in words', () => {
    expect(minutesText(null)).toBe('Not recorded')
    expect(minutesText(17.5)).toBe('17.5 min')
    expect(minutesText(45)).toBe('45 min')
    expect(minutesText(60)).toBe('1 h')
    expect(minutesText(125)).toBe('2 h 5 min')
  })
})
