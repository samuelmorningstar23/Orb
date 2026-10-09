/**
 * Laboratory reports in Bridge Lab History (lis-29, ruling LIS-R11 (c)).
 *
 * Behind ORB_LAB_PORTAL_REPORTS, default off: the server answers 404 and Lab
 * History is exactly what it was. On, the reports the server lets through
 * (a doctor's release or the site's delay) join the list; opening one shows the
 * values with the flag as a printed word and no colour, the tests not yet
 * available with no value, and the Simulated label.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

const reports = vi.fn()
const apiFetch = vi.fn()
vi.mock('../App', () => ({
  useAuth: () => ({ user: { id: 'P001', name: 'Raj Patel', role: 'patient' }, logout: vi.fn() }),
}))
vi.mock('../api/client', () => ({
  getBridgeStatus: () => Promise.resolve({ patient_name: 'Raj Patel', status: 'stable', status_message: 'Monitoring.' }),
  getBridgeReports: () => reports(),
  askBridge: vi.fn(),
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))
vi.mock('../components/bridge/CareTeamCard', () => ({ CareTeamCard: () => null }))
vi.mock('../components/bridge/VitalsTrendsCard', () => ({ VitalsTrendsCard: () => null }))

import PatientShell from './PatientShell'

const REPORTS = {
  patient_id: 'PAT1',
  medications: [],
  clinical_documents: [],
  labs: [
    { doc_type: 'lab_result', type_label: 'Lab result', title: 'Complete Blood Count', content: 'Hb 11.2 g/dL', created_at: '2026-10-01 06:00:00' },
  ],
  imaging_reports: [],
  discharge_summary: null,
}

const LINE = {
  accession: '261009000017',
  specimen_type: 'Serum',
  collected_at: '2026-10-09T03:00:00.000000+00:00',
  reported_at: '2026-10-09T05:00:00.000000+00:00',
  complete: false,
  status_text: 'Some results on this report are not available here yet.',
  simulated: true,
  tests: ['Potassium', 'Sodium'],
  shown_count: 1,
  test_count: 2,
}

const REPORT = {
  accession: LINE.accession,
  specimen_type: 'Serum',
  collected_at: LINE.collected_at,
  reported_at: LINE.reported_at,
  complete: false,
  status_text: LINE.status_text,
  simulated: true,
  simulated_text: 'Simulated result, not from your care',
  tests: [
    {
      name: 'Potassium', panel_name: null, state: 'shown', state_text: '',
      value_text: '6.8', unit: 'mmol/L', flag_text: 'Critically high',
      reference_range_text: '3.5 to 5.1 mmol/L', comment: 'Repeat advised',
      reported_at: LINE.reported_at, corrected: true, corrected_text: 'Corrected by the laboratory',
      earlier: [{ value_text: '7.4', unit: 'mmol/L', replaced_at: LINE.reported_at }],
      simulated: true,
    },
    {
      name: 'Sodium', panel_name: null, state: 'waiting', state_text: 'Not available here yet',
      value_text: null, unit: null, flag_text: '', reference_range_text: null, comment: null,
      reported_at: null, corrected: false, corrected_text: null, earlier: [], simulated: false,
    },
  ],
  signatories: [{ name: 'Dr Rao', registration_text: 'MCI-1' }],
  notes: ['These are the laboratory\'s results as it reported them. Ask your doctor or nurse what they mean for you.'],
}

const NOTE = 'Laboratory reports appear here after your doctor has released them to you.'

function notFound() {
  const err = new Error('This feature is switched off on this site.') as Error & { status: number; detail: unknown }
  err.status = 404
  err.detail = { error: 'feature_disabled', feature: 'lab_portal_reports' }
  return Promise.reject(err)
}

beforeEach(() => {
  reports.mockReset()
  apiFetch.mockReset()
  reports.mockResolvedValue(REPORTS)
})

function serve(lab: 'off' | 'on' | 'failed') {
  apiFetch.mockImplementation((path: string) => {
    if (path === '/appointments/mine') return Promise.resolve({ appointments: [] })
    if (path === '/bridge/lab-reports') {
      if (lab === 'off') return notFound()
      if (lab === 'failed') return Promise.reject(Object.assign(new Error('Server error'), { status: 500 }))
      return Promise.resolve({ reports: [LINE], note: NOTE })
    }
    if (path === `/bridge/lab-reports/${LINE.accession}`) return Promise.resolve({ report: REPORT })
    return Promise.resolve({})
  })
}

async function openLabHistory() {
  render(<PatientShell />)
  await waitFor(() => expect(reports).toHaveBeenCalled())
  await waitFor(() => expect(apiFetch.mock.calls.some(([p]) => p === '/bridge/lab-reports')).toBe(true))
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Lab History/ })) })
}

describe('Bridge laboratory reports', () => {
  it('draws Lab History exactly as before when the site has them switched off', async () => {
    serve('off')
    await openLabHistory()
    const names = Array.from(document.querySelectorAll('.bridge__report-item-name')).map(n => n.textContent)
    expect(names).toEqual(['Complete Blood Count'])
    expect(screen.queryByTestId('portal-lab-note')).toBeNull()
    expect(screen.queryByText(/could not be loaded/)).toBeNull()
    expect(document.body.textContent).not.toMatch(/Laboratory report/)
  })

  it('lists a released report beside the charted results, newest first, with the note', async () => {
    serve('on')
    await openLabHistory()
    const names = Array.from(document.querySelectorAll('.bridge__report-item-name')).map(n => n.textContent)
    expect(names).toEqual(['Potassium, Sodium', 'Complete Blood Count'])
    expect(screen.getByTestId('portal-lab-note').textContent).toBe(NOTE)
    expect(screen.getByText(/^Laboratory report · .*Some results not available here yet · Simulated result, not from your care$/)).toBeInTheDocument()
  })

  it('opens a report with words for the flag, no colour, and no value for a test not available', async () => {
    serve('on')
    await openLabHistory()
    await act(async () => { fireEvent.click(screen.getByText('Potassium, Sodium')) })
    const body = await screen.findByTestId('portal-lab-report')
    expect(apiFetch).toHaveBeenCalledWith(`/bridge/lab-reports/${LINE.accession}`)
    expect(body.textContent).toContain('Result: 6.8 mmol/L')
    expect(body.textContent).toContain('Flag: Critically high')
    expect(body.textContent).toContain('Reference range: 3.5 to 5.1 mmol/L')
    expect(body.textContent).toContain('Laboratory comment: Repeat advised')
    expect(body.textContent).toContain('Corrected by the laboratory on ')
    expect(body.textContent).toContain('Result shown here before: 7.4 mmol/L, corrected ')
    expect(body.textContent).toContain('Not available here yet')
    expect(body.textContent).toContain('Simulated result, not from your care')
    expect(body.textContent).toContain('Authorised by Dr Rao (MCI-1)')
    expect(screen.getByText('Laboratory report: Serum')).toBeInTheDocument()
    // No colour anywhere in the report: no status class and no status colour.
    for (const el of [body, ...Array.from(body.querySelectorAll<HTMLElement>('*'))]) {
      expect(el.getAttribute('class') ?? '').not.toMatch(/status|critical|badge|flag/i)
      expect(el.style.color === '' || el.style.color === 'var(--text-primary)').toBe(true)
      expect(el.style.background).toBe('')
      expect(el.style.opacity).toBe('')
    }
    expect(body.textContent).not.toContain(String.fromCharCode(0x2014))
  })

  it('says the reports could not be loaded rather than that there are none', async () => {
    serve('failed')
    await openLabHistory()
    expect(screen.getByRole('status').textContent).toMatch(/laboratory reports could not be loaded just now/)
    const names = Array.from(document.querySelectorAll('.bridge__report-item-name')).map(n => n.textContent)
    expect(names).toEqual(['Complete Blood Count'])
  })

  it('says a report could not be loaded when opening it fails', async () => {
    serve('on')
    apiFetch.mockImplementation((path: string) => {
      if (path === '/bridge/lab-reports') return Promise.resolve({ reports: [LINE], note: NOTE })
      if (path.startsWith('/bridge/lab-reports/')) return notFound()
      return Promise.resolve({ appointments: [] })
    })
    await openLabHistory()
    await act(async () => { fireEvent.click(screen.getByText('Potassium, Sodium')) })
    expect(await screen.findByText('This report could not be loaded. Please try again later.')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('6.8')
  })
})
