/**
 * The chart's reports and the discharge screen, gap fixes of 2026-10-03:
 *   - F041: a filed Lens read shows the text the clinician confirmed and loads
 *     its image from the authenticated /uploads URL (image_path is a location
 *     on the appliance's disk and rendered as a broken image).
 *   - F044: a simulator's lab or imaging result is labelled "Simulated".
 *   - F050: open escalation chains are listed on the discharge screen to be
 *     closed with a reason.
 */
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { openPatient } from '../../lib/orbBus'

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: vi.fn(async () => ({ channelId: null, assessment: null, rows: [] })),
  listSageAssessments: vi.fn(async () => []),
  transitionAssessment: vi.fn(),
}))

const state: { detail: unknown } = { detail: null }

const LAB_TEXT = 'BMP\n  Creatinine: 2.4 mg/dL  [H]  (ref: 0.7-1.3)'

function detail(over: Record<string, unknown> = {}) {
  return {
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
      { id: 7, doc_type: 'lab_result', title: 'BMP (STAT)', content: LAB_TEXT, created_at: '2026-10-02 08:15:00', source: 'simulated' },
    ],
    scribe_notes: [],
    lens_analyses: [
      { id: 31, image_type: 'xray', clinical_context: 'Cough, 3 days', analysis: 'RAW MODEL READ',
        reviewed_text: 'Right lower lobe opacity, confirmed', reviewed_at: '2026-10-02T09:30:00+00:00',
        image_path: '/Users/orb/server/uploads/lens_abc.png', image_url: '/uploads/lens_abc.png',
        created_at: '2026-10-02 09:20:00', source: null },
      { id: 32, image_type: 'CT', clinical_context: 'Abdominal CT', analysis: 'Template finding',
        reviewed_text: null, reviewed_at: null,
        image_path: '/uploads/mock_ct.png', image_url: '/uploads/mock_ct.png',
        created_at: '2026-10-02 07:00:00', source: 'simulated' },
    ],
    lab_observations: [],
    problems: [],
    site_name: null,
    risk: { level: 'stable', score: 0.1, raw_news2: 1, drivers: [], breakdown: [], single_param_3: false },
    trend: { trend: 'stable', reason: null },
    ...over,
  }
}

const apiFetch = vi.fn(async (path: string): Promise<unknown> => {
  if (path.includes('/allergies')) return { allergies: [] }
  if (path.includes('/discharge-advisory')) return 'unavailable'
  return {}
})
const getOpenEscalations = vi.fn(async () => [
  { id: 'ESC-1', patient_id: 'P021', status: 'acknowledged', raised_at: '2026-10-02T03:00:00Z', peak_news2: 7 },
])
const resolveEscalation = vi.fn<(id: string, reason: string) => Promise<{ id: string; status: string }>>(
  async () => ({ id: 'ESC-1', status: 'resolved' }),
)

vi.mock('../../api/client', () => ({
  apiFetch: (path: string) => apiFetch(path),
  getPatientDetail: vi.fn(async () => state.detail),
  getAdministrations: vi.fn(async () => []),
  givenAcrossAllergy: () => false,
  dischargePatient: vi.fn(),
  getOpenEscalations: () => getOpenEscalations(),
  resolveEscalation: (id: string, reason: string) => resolveEscalation(id, reason),
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
import { buildLabTrends } from '../../lib/labTrends'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

beforeEach(() => {
  vi.clearAllMocks()
  state.detail = detail()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

async function openChart() {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
  await act(async () => { openPatient('P021', 'Ravi Kumar') })
  await screen.findByText('Ravi Kumar')
}

describe('Lens reads on the chart', () => {
  it('shows the clinician-reviewed text and loads the image from its /uploads URL', async () => {
    await openChart()
    fireEvent.click(document.getElementById('btn-diag-reports') as HTMLElement)
    fireEvent.click(await screen.findByText('Cough, 3 days'))

    const dialog = await screen.findByRole('dialog', { name: /Diagnostic Report/ })
    expect(within(dialog).getByText('Right lower lobe opacity, confirmed')).toBeInTheDocument()
    expect(within(dialog).queryByText('RAW MODEL READ')).toBeNull()
    expect(within(dialog).getByText('CLINICIAN-REVIEWED FINDINGS')).toBeInTheDocument()
    const img = within(dialog).getByRole('img', { name: 'Medical scan' })
    expect(img).toHaveAttribute('src', '/uploads/lens_abc.png')
  })

  it('labels an unreviewed simulated read as both', async () => {
    await openChart()
    fireEvent.click(document.getElementById('btn-diag-reports') as HTMLElement)
    const list = await screen.findByRole('dialog', { name: /Diagnostic Reports/ })
    expect(within(list).getAllByText('Simulated')).toHaveLength(1)
    fireEvent.click(within(list).getByText('Abdominal CT'))
    const dialog = await screen.findByRole('dialog', { name: /Diagnostic Report/ })
    expect(within(dialog).getByText('AI FINDINGS, NOT REVIEWED BY A CLINICIAN')).toBeInTheDocument()
    expect(within(dialog).getByText('Simulated')).toBeInTheDocument()
  })
})

describe('simulated lab results', () => {
  it('marks the trend from a simulated report as simulated', () => {
    const [trend] = buildLabTrends(detail().notes, [])
    expect(trend.name).toBe('Creatinine')
    expect(trend.simulated).toBe(true)
    const [genuine] = buildLabTrends([{ ...detail().notes[0], source: null }], [])
    expect(genuine.simulated).toBe(false)
  })

  it('badges the lab report card', async () => {
    await openChart()
    fireEvent.click(document.getElementById('btn-lab-reports') as HTMLElement)
    const list = await screen.findByRole('dialog', { name: /Lab Reports/ })
    expect(within(list).getAllByText('Simulated').length).toBeGreaterThanOrEqual(1)
  })
})

describe('discharge screen', () => {
  it('lists an open escalation and closes it only with a reason', async () => {
    await openChart()
    fireEvent.click(screen.getByRole('button', { name: /^Discharge$/ }))
    const box = await screen.findByTestId('discharge-open-escalations')
    expect(box).toHaveTextContent('1 deterioration escalation is still open')
    expect(box).toHaveTextContent('This does not prevent the discharge.')

    const close = within(box).getByRole('button', { name: 'Close escalation' })
    expect(close).toBeDisabled()
    fireEvent.change(within(box).getByLabelText('Reason for closing'), { target: { value: 'Reviewed, fit for discharge' } })
    await waitFor(() => expect(close).toBeEnabled())
    await act(async () => { fireEvent.click(close) })

    expect(resolveEscalation).toHaveBeenCalledWith('ESC-1', 'Reviewed, fit for discharge')
    await waitFor(() => expect(screen.queryByTestId('discharge-open-escalations')).toBeNull())
    // The discharge itself is still offered.
    expect(screen.getByRole('button', { name: /Confirm discharge/ })).toBeEnabled()
  })
})
