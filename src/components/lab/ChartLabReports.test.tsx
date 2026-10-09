/**
 * The laboratory's reports in the chart's Labs dialog (lis-14).
 *
 *   1. The list is the server's, loaded once: tests, report state, accession,
 *      pending and flagged counts, the Simulated label, the time through
 *      src/lib/datetime.ts.
 *   2. The dialog's search box filters it by keyword or date.
 *   3. An account the laboratory does not answer (403), or a laboratory that
 *      is off (404), shows NO section; any other failure says so in words and
 *      offers to try again; it is never drawn as an empty list.
 *   4. Opening a report reads it once (audited on the server); printing asks
 *      for the print copy (audited as a print).
 *   5. No em dash in anything it writes.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../api/lab'
import type { LabReport, ReportSummary } from '../../api/lab/reports'
import { formatClinicalDateTime } from '../../lib/datetime'

const listPatientLabReports = vi.fn()
const getLabReport = vi.fn()
const printLabReport = vi.fn()

vi.mock('../../api/lab/reports', () => ({
  listPatientLabReports: (id: string) => listPatientLabReports(id),
  getLabReport: (a: string) => getLabReport(a),
  printLabReport: (a: string) => printLabReport(a),
}))

vi.mock('../print/LabReportPrintView', () => ({
  default: ({ report, variant }: { report: LabReport; variant?: string }) => (
    <div data-testid={`report-${variant ?? 'print'}`}>Report {report.accession}</div>
  ),
}))

import ChartLabReports from './ChartLabReports'

function summary(over: Partial<ReportSummary> = {}): ReportSummary {
  return {
    accession: '261009000123',
    status: 'final',
    status_text: 'Final report',
    amended: false,
    simulated: false,
    pending_count: 0,
    patient: { id: 'P021', name: 'Ravi Kumar', mrn: 'MRN-0021', ward: 'GM-2', bed: '21' },
    specimen_type: 'Serum',
    priority: 'routine',
    collected_at: '2026-10-09T03:00:00Z',
    reported_at: '2026-10-09T05:00:00Z',
    last_reported_at: '2026-10-09T05:00:00Z',
    tests: ['Sodium', 'Potassium'],
    flagged_count: 1,
    ...over,
  }
}

const REPORTS = [
  summary(),
  summary({ accession: '261008000045', tests: ['Haemoglobin'], status: 'interim',
    status_text: 'Interim report', pending_count: 2, flagged_count: 0, simulated: true,
    last_reported_at: '2026-10-08T05:00:00Z', specimen_type: 'Whole blood' }),
]

beforeEach(() => {
  vi.clearAllMocks()
  listPatientLabReports.mockResolvedValue({ patient_id: 'P021', reports: REPORTS, cap: 200 })
  getLabReport.mockImplementation(async (a: string) => ({ accession: a } as LabReport))
  printLabReport.mockImplementation(async (a: string) => ({ accession: a } as LabReport))
})

describe('the laboratory reports in the chart', () => {
  it('lists every report the laboratory holds for the patient, once', async () => {
    const onCount = vi.fn()
    render(<ChartLabReports patientId="P021" query="" onCount={onCount} />)
    expect(screen.getByText('From the laboratory')).toBeInTheDocument()
    expect(await screen.findByText('Sodium, Potassium')).toBeInTheDocument()
    expect(screen.getByText('Haemoglobin')).toBeInTheDocument()
    expect(screen.getByText(/Final report · Accession 261009 00012 3 · Serum · 1 result flagged/))
      .toBeInTheDocument()
    expect(screen.getByText(/Interim report · Accession 261008 00004 5 · Whole blood · 2 tests pending/))
      .toBeInTheDocument()
    expect(screen.getByText('Simulated')).toBeInTheDocument()
    expect(screen.getByText(formatClinicalDateTime('2026-10-09T05:00:00Z'))).toBeInTheDocument()
    expect(listPatientLabReports).toHaveBeenCalledTimes(1)
    expect(listPatientLabReports).toHaveBeenCalledWith('P021')
    await waitFor(() => expect(onCount).toHaveBeenLastCalledWith(2))
  })

  it('filters by keyword and by date', async () => {
    const { rerender } = render(<ChartLabReports patientId="P021" query="haemo" />)
    expect(await screen.findByText('Haemoglobin')).toBeInTheDocument()
    expect(screen.queryByText('Sodium, Potassium')).toBeNull()
    rerender(<ChartLabReports patientId="P021" query="9 Oct 2026" />)
    expect(screen.getByText('Sodium, Potassium')).toBeInTheDocument()
    expect(screen.queryByText('Haemoglobin')).toBeNull()
    rerender(<ChartLabReports patientId="P021" query="troponin" />)
    expect(screen.getByText('No laboratory report matches your search.')).toBeInTheDocument()
    expect(listPatientLabReports).toHaveBeenCalledTimes(1)
  })

  it('shows nothing at all to an account the laboratory does not answer', async () => {
    listPatientLabReports.mockRejectedValue(new LabApiError('Forbidden', 403, 'permission_denied'))
    const onCount = vi.fn()
    const { container } = render(<ChartLabReports patientId="P021" query="" onCount={onCount} />)
    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(onCount).toHaveBeenLastCalledWith(0)
  })

  it('shows nothing while the laboratory is switched off', async () => {
    listPatientLabReports.mockRejectedValue(new LabApiError('Off', 404, 'module_disabled'))
    const { container } = render(<ChartLabReports patientId="P021" query="" />)
    await waitFor(() => expect(container).toBeEmptyDOMElement())
  })

  it('says a failure in words and tries again, never an empty list', async () => {
    listPatientLabReports.mockRejectedValueOnce(
      new LabApiError('Could not reach the laboratory. Check the connection and try again.', 0, 'network_error'))
    render(<ChartLabReports patientId="P021" query="" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the laboratory')
    expect(screen.queryByText('The laboratory has no report for this patient.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Sodium, Potassium')).toBeInTheDocument()
    expect(listPatientLabReports).toHaveBeenCalledTimes(2)
  })

  it('says so when the laboratory has no report for the patient', async () => {
    listPatientLabReports.mockResolvedValue({ patient_id: 'P021', reports: [], cap: 200 })
    render(<ChartLabReports patientId="P021" query="" />)
    expect(await screen.findByText('The laboratory has no report for this patient.')).toBeInTheDocument()
  })

  it('opens a report once and prints the print copy', async () => {
    const onOpenChange = vi.fn()
    render(<ChartLabReports patientId="P021" query="" onOpenChange={onOpenChange} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open laboratory report 261009 00012 3' }))
    expect(await screen.findByTestId('report-screen')).toHaveTextContent('Report 261009000123')
    expect(getLabReport).toHaveBeenCalledTimes(1)
    expect(getLabReport).toHaveBeenCalledWith('261009000123')
    await waitFor(() => expect(onOpenChange).toHaveBeenLastCalledWith(true))

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Print report' })) })
    expect(printLabReport).toHaveBeenCalledWith('261009000123')
    expect(await screen.findByTestId('report-print')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Back to the list' }))
    expect(await screen.findByText('Sodium, Potassium')).toBeInTheDocument()
    await waitFor(() => expect(onOpenChange).toHaveBeenLastCalledWith(false))
    expect(listPatientLabReports).toHaveBeenCalledTimes(1)
  })

  it('says when a report can no longer be opened', async () => {
    getLabReport.mockRejectedValue(new LabApiError('Not found', 404, 'not_found'))
    render(<ChartLabReports patientId="P021" query="" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open laboratory report 261009 00012 3' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This report is no longer available to this account.')
    expect(screen.queryByRole('button', { name: 'Print report' })).toBeNull()
  })

  it('writes no em dash', async () => {
    const { container } = render(<ChartLabReports patientId="P021" query="" />)
    await screen.findByText('Sodium, Potassium')
    expect(container.textContent).not.toContain('\u2014')
  })
})
