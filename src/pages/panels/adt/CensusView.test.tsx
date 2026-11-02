/**
 * The Beds panel's Census view (adt-13a).
 *
 * What it must never get wrong:
 *
 * 1. AN INSUFFICIENT DAY IS NEVER A ZERO: it prints 'Insufficient data' and
 *    the server's reason, and no count.
 * 2. THE METHODS ARE PRINTED as the server wrote them, with both occupancy
 *    figures beside the NABH one (ruling ADT-P4).
 * 3. HOSPITAL TOTALS appear only when the server sends them; a department
 *    account reads why not.
 * 4. A REFUSED PERIOD keeps the census on screen and says why; the module
 *    switched off hands the panel its notice; Download CSV fetches the file.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn<(path: string, init?: RequestInit) => Promise<unknown>>()

vi.mock('../../../api/client', async () => ({
  ...await vi.importActual<typeof import('../../../api/client')>('../../../api/client'),
  API_BASE: '/api',
  apiFetch: (path: string, init?: RequestInit) => apiFetch(path, init),
  buildHeaders: () => ({}),
  handleResponseError: async () => { throw new Error('refused') },
  errorDetailMessage: (detail: unknown, status: number) => {
    if (typeof detail === 'string') return detail
    const message = (detail as { message?: unknown } | null)?.message
    return typeof message === 'string' ? message : `The server answered ${status}.`
  },
  getUser: () => ({ role: 'admin' }),
}))

import CensusView, { viewId, viewLabel, viewPermission } from './CensusView'
import type { Census } from '../../../api/adt/census'

const COMPLETE_DAY = {
  date: '2026-10-07', status: 'complete' as const, midnight_census: 7, admissions: 2,
  transfers_in: 1, transfers_out: 0, ends: { routine: 1, death: 1 }, deaths: 1,
  inpatient_days: 6.75, stays: 9, licensed_beds: 10, available_beds: 9.75,
  occupancy: { nabh: 71.8, licensed: 70, hours: 69.2 },
}

const CENSUS: Census = {
  generated_at: '2026-10-09T05:00:00Z', from: '2026-10-06', to: '2026-10-07',
  timezone: 'Asia/Kolkata', department: null, ward_id: null,
  ward_choices: [{ id: 'WRD-GM', code: 'GM-1', name: 'General ward', department: 'General Medicine' }],
  wards: [{
    id: 'WRD-GM', code: 'GM-1', name: 'General ward', department: 'General Medicine',
    ward_type: 'general', is_active: true, icu: false, ledger_complete_from: '2026-10-06T10:00:00Z',
    first_complete_day: '2026-10-07',
    days: [
      { date: '2026-10-06', status: 'insufficient_data',
        reason: 'GM-1: figures start on the first full day after its bed register was marked complete in Bed setup.' },
      COMPLETE_DAY,
    ],
    totals: { status: 'partial', complete_days: 1, insufficient_days: 1, admissions: 2,
              average_midnight_census: 7, transfers_in: 1, transfers_out: 0,
              ends: { routine: 1, death: 1 }, deaths: 1, inpatient_days: 6.75,
              occupancy: { nabh: 71.8, licensed: 70, hours: 69.2 }, stays_ended: 2, alos_days: 3.5 },
  }],
  hospital: {
    days: [
      { date: '2026-10-06', status: 'insufficient_data', reason: 'Hospital totals need every ward complete for the day. Not complete: GM-1.' },
      { ...COMPLETE_DAY, not_on_a_ward: 1, icu: { transfers_out: 0, returns_48h: 0, pending: 0 } },
    ],
    totals: { status: 'partial', complete_days: 1, insufficient_days: 1 },
  },
  end_type_words: { routine: 'Routine discharge', death: 'Death' },
  methods: {
    midnight_census: 'Patients present at the stroke of midnight that ends the day.',
    occupancy: 'NABH occupancy is the midnight census over available beds.',
  },
}

function refusal(status: number, detail: unknown) {
  return Object.assign(new Error((detail as { message?: string })?.message ?? 'refused'),
    { status, detail })
}

const PROPS = { meta: {}, openView: vi.fn(), holds: () => true }

beforeEach(() => {
  apiFetch.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

async function renderView(onModuleOff = vi.fn()) {
  await act(async () => {
    render(<CensusView {...PROPS} onModuleOff={onModuleOff} />)
  })
  return onModuleOff
}

describe('Census view', () => {
  it('is installed as Census, for adt.read', () => {
    expect([viewId, viewLabel, viewPermission]).toEqual(['census', 'Census', 'adt.read'])
  })

  it('prints an insufficient day as words, never a zero, and the complete day in full', async () => {
    apiFetch.mockResolvedValue(CENSUS)
    await renderView()
    const table = screen.getByRole('table', { name: 'GM-1, day by day' })
    const rows = within(table).getAllByRole('row')
    expect(rows[1]).toHaveTextContent('Insufficient data.')
    expect(rows[1]).toHaveTextContent('figures start on the first full day')
    expect(within(rows[1]).queryByText('0')).toBeNull()
    expect(rows[2]).toHaveTextContent('2: 1 Routine discharge, 1 Death')
    expect(rows[2]).toHaveTextContent('71.8%')
    expect(screen.getByText('Counted from 7 Oct 2026')).toBeInTheDocument()
    expect(screen.getAllByText(/Totals add the 1 complete days/).length).toBeGreaterThan(0)
  })

  it('shows both occupancy figures beside the NABH one, and the methods as written', async () => {
    apiFetch.mockResolvedValue(CENSUS)
    await renderView()
    const ward = screen.getByRole('region', { name: /GM-1/ })
    const totals = ward.querySelector('dl[aria-label="Totals for the period"]') as HTMLElement
    expect(within(totals).getByText('Occupancy, NABH (midnight census over available beds)')
      .nextSibling).toHaveTextContent('71.8%')
    expect(within(totals).getByText('Midnight census over licensed beds').nextSibling)
      .toHaveTextContent('70%')
    expect(within(totals).getByText('Inpatient days over available beds').nextSibling)
      .toHaveTextContent('69.2%')
    expect(within(totals).getByText('Average stay').nextSibling).toHaveTextContent('3.5 days')
    expect(screen.getByText('How these figures are counted')).toBeInTheDocument()
    expect(screen.getByText('NABH occupancy is the midnight census over available beds.')).toBeInTheDocument()
  })

  it('shows the hospital only when sent, and says why to a department account', async () => {
    apiFetch.mockResolvedValue(CENSUS)
    await renderView()
    expect(screen.getByRole('heading', { name: 'Whole hospital' })).toBeInTheDocument()
    const hospitalTable = screen.getByRole('table', { name: 'The whole hospital, day by day' })
    expect(within(hospitalTable).getByText('Not on a ward')).toBeInTheDocument()
  })

  it('tells a department account why there are no hospital totals', async () => {
    apiFetch.mockResolvedValue({ ...CENSUS, hospital: null, department: 'General Medicine',
      hospital_hidden: 'Hospital totals are shown to accounts that are not tied to one department.' })
    await renderView()
    expect(screen.queryByRole('heading', { name: 'Whole hospital' })).toBeNull()
    expect(screen.getByText(/not tied to one department/)).toBeInTheDocument()
  })

  it('keeps the census on screen when a period is refused, and says why', async () => {
    apiFetch.mockResolvedValueOnce(CENSUS)
    apiFetch.mockRejectedValueOnce(refusal(422, { error: 'day_not_ended',
      message: 'The census counts whole days, and today has not ended yet.' }))
    await renderView()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Show' })) })
    expect(screen.getByRole('alert')).toHaveTextContent('today has not ended yet')
    expect(screen.getByRole('table', { name: 'GM-1, day by day' })).toBeInTheDocument()
    const sent = apiFetch.mock.calls[1][0]
    expect(sent).toContain('/adt/census?')
    expect(sent).toContain('from=2026-10-06')
    expect(sent).toContain('to=2026-10-07')
  })

  it('hands the panel its notice when the module is switched off', async () => {
    apiFetch.mockRejectedValue(refusal(404, { error: 'module_disabled', module: 'adt',
      message: 'Beds is switched off' }))
    const onModuleOff = await renderView()
    expect(onModuleOff).toHaveBeenCalled()
  })

  it('downloads the CSV through the session', async () => {
    apiFetch.mockResolvedValue(CENSUS)
    const fetchMock = vi.fn(async () => new Response('level,ward\n', { status: 200,
      headers: { 'Content-Type': 'text/csv' } }))
    vi.stubGlobal('fetch', fetchMock)
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const created = vi.fn(() => 'blob:census')
    const revoked = vi.fn()
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: revoked })
    await renderView()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Download CSV' })) })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][]
    expect(calls[0][0]).toContain('/api/adt/census.csv?from=2026-10-06&to=2026-10-07')
    expect(calls[0][1]).toMatchObject({ credentials: 'include' })
    expect(created).toHaveBeenCalled()
    expect(revoked).toHaveBeenCalledWith('blob:census')
    expect(clicked).toHaveBeenCalledTimes(1)
    clicked.mockRestore()
  })
})
