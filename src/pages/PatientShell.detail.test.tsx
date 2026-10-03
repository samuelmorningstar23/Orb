/**
 * Patient portal: a row's detail opens ABOVE the list it was opened from, and
 * Escape closes only the detail.
 *
 * The detail overlay sat at z-index 100 under the list's 9999, so opening a
 * medication, note or lab looked like it did nothing; and both dialogs' focus
 * traps handled every Escape, so one key press closed both.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const reports = vi.fn()
vi.mock('../App', () => ({
  useAuth: () => ({ user: { id: 'P001', name: 'Raj Patel', role: 'patient' }, logout: vi.fn() }),
}))
vi.mock('../api/client', () => ({
  getBridgeStatus: () => Promise.resolve({ patient_name: 'Raj Patel', status: 'stable', status_message: 'Monitoring.' }),
  getBridgeReports: () => reports(),
  askBridge: vi.fn(),
  apiFetch: () => Promise.resolve({ appointments: [] }),
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
    { doc_type: 'lab_result', type_label: 'Lab result', title: 'Basic Metabolic Panel', content: 'K 6.1', created_at: '2026-10-01 07:00:00', source: 'simulated' },
  ],
  imaging_reports: [],
  discharge_summary: null,
}

beforeEach(() => {
  reports.mockReset()
  reports.mockResolvedValue(REPORTS)
})

function zIndexOf(css: string, selector: string): number {
  const at = css.indexOf(`${selector} {`)
  expect(at).toBeGreaterThanOrEqual(0)
  const block = css.slice(at, css.indexOf('}', at))
  const m = /z-index:\s*(\d+)/.exec(block)
  expect(m).not.toBeNull()
  return Number(m![1])
}

describe('portal detail dialog', () => {
  it('stacks the detail overlay above the list overlay', () => {
    const css = fs.readFileSync(path.join(__dirname, 'PatientShell.css'), 'utf8')
    expect(zIndexOf(css, '.bridge__report-modal-overlay')).toBeGreaterThan(zIndexOf(css, '.modal-overlay'))
  })

  it('opens over the list, and Escape closes only the detail', async () => {
    render(<PatientShell />)
    await waitFor(() => expect(reports).toHaveBeenCalled())
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Lab History/ })) })
    fireEvent.click(await screen.findByText('Complete Blood Count'))

    const dialogs = await screen.findAllByRole('dialog')
    expect(dialogs).toHaveLength(2)
    // The detail is the later dialog, and it has a named close control.
    const detail = dialogs[1]
    expect(detail).toHaveAttribute('aria-labelledby', 'portal-detail-title')
    expect(within(detail).getByRole('button', { name: 'Close' })).toBeInTheDocument()

    await act(async () => { fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' }) })
    await waitFor(() => expect(screen.getAllByRole('dialog')).toHaveLength(1), { timeout: 2000 })
    expect(screen.getByText('Lab & Imaging History')).toBeInTheDocument()
  })

  it('labels a simulated result for the patient', async () => {
    render(<PatientShell />)
    await waitFor(() => expect(reports).toHaveBeenCalled())
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Lab History/ })) })
    expect(await screen.findByText(/Simulated result, not from your care/)).toBeInTheDocument()
    expect(screen.getAllByText(/Simulated result/)).toHaveLength(1)
  })
})
