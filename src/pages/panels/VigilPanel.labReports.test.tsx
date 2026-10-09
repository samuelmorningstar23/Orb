/**
 * The chart's Labs dialog lists the laboratory's reports (lis-14, HIS wave 8).
 *
 *   * With Orb's laboratory off the dialog is exactly as it was: the results
 *     filed in the chart, its own count and empty words, and no call to the
 *     laboratory at all.
 *   * With it on (or draining) the laboratory's reports for this patient are
 *     listed above the results filed in the chart, the count adds them, and
 *     opening one replaces the dialog's lists with the report until Back.
 *   * An account the laboratory does not answer sees the dialog as before.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { openPatient } from '../../lib/orbBus'
import { LabApiError } from '../../api/lab'
import type { LabReport } from '../../api/lab/reports'

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: vi.fn(async () => ({ channelId: null, assessment: null, rows: [] })),
  listSageAssessments: vi.fn(async () => []),
  transitionAssessment: vi.fn(),
}))

const listPatientLabReports = vi.fn()
const getLabReport = vi.fn()
vi.mock('../../api/lab/reports', () => ({
  listPatientLabReports: (id: string) => listPatientLabReports(id),
  getLabReport: (a: string) => getLabReport(a),
  printLabReport: vi.fn(),
}))
vi.mock('../../components/print/LabReportPrintView', () => ({
  default: ({ report }: { report: LabReport }) => <div data-testid="lab-report">Report {report.accession}</div>,
}))

const DETAIL = {
  patient: {
    id: 'P021', name: 'Ravi Kumar', bed: '21', mrn: 'MRN-0021', ward: 'GM-2',
    diagnosis: 'Community acquired pneumonia', admission_date: '2026-09-28 06:30:00',
    age: 58, sex: 'M', comorbidities: [], admission_id: 'ADM-0021',
  },
  vitals: [{
    timestamp: '2026-10-02T09:00:00Z', heart_rate: 96, systolic_bp: 118,
    diastolic_bp: 70, respiratory_rate: 18, temperature: 37.4, spo2: 96,
  }],
  medications: [],
  notes: [
    { id: 7, doc_type: 'lab_result', title: 'BMP (STAT)', content: 'BMP\n  Creatinine: 2.4 mg/dL', created_at: '2026-10-02 08:15:00', source: null },
  ],
  scribe_notes: [],
  lens_analyses: [],
  lab_observations: [],
  problems: [],
  site_name: null,
  risk: { level: 'stable', score: 0.1, raw_news2: 1, drivers: [], breakdown: [], single_param_3: false },
  trend: { trend: 'stable', reason: null },
}

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async (path: string) => (path.includes('/allergies') ? { allergies: [] } : {})),
  getPatientDetail: vi.fn(async () => DETAIL),
  getAdministrations: vi.fn(async () => []),
  givenAcrossAllergy: () => false,
  dischargePatient: vi.fn(),
  getOpenEscalations: vi.fn(async () => []),
  resolveEscalation: vi.fn(),
  addClinicalNote: vi.fn(),
  addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getDischargeQrObjectUrl: vi.fn(async () => 'blob:qr'),
  prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  transitionSageAction: vi.fn(),
  getChannels: vi.fn(async () => []),
  getUser: () => ({ id: 'DOC1', name: 'Dr Meera Raman', role: 'doctor' }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

vi.mock('../../components/sage/SageInsightsFeed', () => ({ default: () => null }))
vi.mock('../../components/sage/SageAlertAssessment', () => ({ default: () => null }))
vi.mock('../../components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('../../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import VigilPanel from './VigilPanel'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'
import { resetModuleFlags, setModuleFlags } from '../../shellConfig'

const REPORT = {
  accession: '261009000123', status: 'final', status_text: 'Final report', amended: false,
  simulated: false, pending_count: 0,
  patient: { id: 'P021', name: 'Ravi Kumar', mrn: 'MRN-0021', ward: 'GM-2', bed: '21' },
  specimen_type: 'Serum', priority: 'routine', collected_at: '2026-10-09T03:00:00Z',
  reported_at: '2026-10-09T05:00:00Z', last_reported_at: '2026-10-09T05:00:00Z',
  tests: ['Sodium', 'Potassium'], flagged_count: 0,
}

beforeEach(() => {
  vi.clearAllMocks()
  listPatientLabReports.mockResolvedValue({ patient_id: 'P021', reports: [REPORT], cap: 200 })
  getLabReport.mockImplementation(async (a: string) => ({ accession: a }))
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

afterEach(() => {
  resetModuleFlags()
})

async function openLabs() {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
  await act(async () => { openPatient('P021', 'Ravi Kumar') })
  await screen.findByText('Ravi Kumar')
  fireEvent.click(document.getElementById('btn-lab-reports') as HTMLElement)
  return screen.findByRole('dialog', { name: /Lab Reports/ })
}

describe('the Labs dialog and the laboratory', () => {
  it('is exactly as before with the laboratory off', async () => {
    setModuleFlags({ lab: false })
    const dialog = await openLabs()
    expect(within(dialog).getByText('1 total')).toBeInTheDocument()
    expect(within(dialog).getByText('BMP (STAT)')).toBeInTheDocument()
    expect(within(dialog).queryByText('From the laboratory')).toBeNull()
    expect(within(dialog).queryByText('Filed in the chart')).toBeNull()
    expect(listPatientLabReports).not.toHaveBeenCalled()
  })

  it('lists the laboratory reports above the results filed in the chart', async () => {
    setModuleFlags({ lab: true })
    const dialog = await openLabs()
    expect(await within(dialog).findByText('Sodium, Potassium')).toBeInTheDocument()
    expect(within(dialog).getByText('From the laboratory')).toBeInTheDocument()
    expect(within(dialog).getByText('Filed in the chart')).toBeInTheDocument()
    expect(within(dialog).getByText('BMP (STAT)')).toBeInTheDocument()
    await waitFor(() => expect(within(dialog).getByText('2 total')).toBeInTheDocument())
    expect(listPatientLabReports).toHaveBeenCalledWith('P021')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Open laboratory report 261009 00012 3' }))
    expect(await within(dialog).findByTestId('lab-report')).toHaveTextContent('Report 261009000123')
    // The report replaces the lists until Back.
    expect(within(dialog).queryByText('BMP (STAT)')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to the list' }))
    expect(await within(dialog).findByText('BMP (STAT)')).toBeInTheDocument()
  })

  it('shows the dialog as before to an account the laboratory does not answer', async () => {
    setModuleFlags({ lab: true })
    listPatientLabReports.mockRejectedValue(new LabApiError('Forbidden', 403, 'permission_denied'))
    const dialog = await openLabs()
    await waitFor(() => expect(listPatientLabReports).toHaveBeenCalled())
    await waitFor(() => expect(within(dialog).queryByText('From the laboratory')).toBeNull())
    expect(within(dialog).getByText('1 total')).toBeInTheDocument()
    expect(within(dialog).getByText('BMP (STAT)')).toBeInTheDocument()
  })
})
