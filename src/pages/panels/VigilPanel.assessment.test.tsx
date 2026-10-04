/**
 * THE ALERT NEVER WAITS FOR SAGE — asserted on the real chart, not on the card.
 *
 * Vitals-in to clinician-visible alert is 51 ms and that number is the product.
 * Sage's drafted assessment is generated AFTERWARDS, takes ~45 s against the 27B
 * model, and can be shed or fail outright. So the property that matters on this
 * panel is not "the draft renders nicely" — the colocated component suite covers
 * that — it is:
 *
 *   1. the deteriorating patient's NEWS2 band, drivers and vitals are on screen
 *      with the assessment read STILL IN FLIGHT and never resolving;
 *   2. they are on screen when the read resolves to "there is no draft", with
 *      nothing else added — no placeholder, no spinner, no empty box implying
 *      something is coming;
 *   3. they are on screen when the read FAILS;
 *   4. and when a draft does exist it lands BELOW the alert, in the DOM, so it
 *      can never displace or delay it.
 *
 * A regression that made the chart await the draft — a `loading` gate lifted one
 * level, a Suspense boundary around the wrong subtree — would fail (1) while
 * leaving every component test green.
 */
import { render, screen, waitFor, act } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { AdministrationRecord } from '../../api/client'
import { openPatient } from '../../lib/orbBus'

const mocks = vi.hoisted(() => ({
  getLatestAssessmentForPatient: vi.fn(),
  listSageAssessments: vi.fn(),
  transitionAssessment: vi.fn(),
}))

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: mocks.getLatestAssessmentForPatient,
  listSageAssessments: mocks.listSageAssessments,
  transitionAssessment: mocks.transitionAssessment,
}))

/** A genuinely deteriorating patient: NEWS2 19, the figure the pilot dry run
 *  found being displayed as 10. The band, the number and the drivers together
 *  ARE the alert on this surface. */
const DETAIL = {
  patient: {
    patient_id: 'P007', name: 'Rahul Menon', bed: '7', mrn: 'MRN-P007',
    diagnosis: 'Hospital-acquired pneumonia', admission_date: '2026-08-10',
    age: 71, sex: 'M', comorbidities: [],
  },
  vitals: [{
    timestamp: '2026-08-14T09:00:00Z', heart_rate: 128, systolic_bp: 84,
    diastolic_bp: 52, respiratory_rate: 32, temperature: 39.1, spo2: 88,
  }],
  medications: [], notes: [], reports: [], labs: [], alerts: [], lens_analyses: [],
  risk: {
    level: 'critical', score: 0.92, raw_news2: 19,
    drivers: ['Respiratory rate 32 (3 points)', 'SpO2 88% on air (3 points)'],
    breakdown: [], single_param_3: true,
  },
  trend: { trend: 'deteriorating' },
}

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({})),
  getPatientDetail: vi.fn(async () => DETAIL),
  getAdministrations: vi.fn(async () => []),
  // Real semantics, mirrored: only a 'given' dose carrying a recorded
  // override reason is marked as crossing a charted allergy.
  givenAcrossAllergy: (r: AdministrationRecord | null | undefined) => Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
  dischargePatient: vi.fn(),
  getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  addClinicalNote: vi.fn(),
  addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getDischargeQrObjectUrl: vi.fn(),
  prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  transitionSageAction: vi.fn(),
  getChannels: vi.fn(async () => []),
  getUser: () => ({ id: 'DOC1', name: 'Dr Iyer', role: 'doctor' }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

vi.mock('../../components/sage/SageInsightsFeed', () => ({ default: () => null }))
vi.mock('../../components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('../../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import VigilPanel from './VigilPanel'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

function renderPanel() {
  return render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role="doctor" />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
}

/** Open the deteriorating patient's chart and wait for the alert to paint. */
async function openChart() {
  renderPanel()
  await act(async () => { openPatient('P007', 'Rahul Menon') })
  await screen.findByText('Rahul Menon')
}

/** The alert, as this surface expresses it. */
function expectAlertVisible() {
  expect(screen.getByText(/NEWS2 19 · CRITICAL/i)).toBeInTheDocument()
  expect(screen.getByText(/Respiratory rate 32 \(3 points\)/)).toBeInTheDocument()
  expect(screen.getByText(/Deteriorating/)).toBeInTheDocument()
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.listSageAssessments.mockResolvedValue([])
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

describe('Vigil chart: the alert never waits for the drafted assessment', () => {
  it('renders the alert with the assessment read still in flight', async () => {
    // Never resolves — the pathological case: the model is generating, the
    // network is hanging, whatever. The chart must not care.
    mocks.getLatestAssessmentForPatient.mockReturnValue(new Promise(() => {}))

    await openChart()

    expectAlertVisible()
    // And nothing is claiming an assessment is on its way, because we have not
    // yet been told one exists.
    expect(screen.queryByText('AI DRAFT')).not.toBeInTheDocument()
    expect(screen.queryByText(/Sage is writing an assessment/i)).not.toBeInTheDocument()
  })

  it('renders the alert alone when there is no draft — no placeholder, no spinner', async () => {
    mocks.getLatestAssessmentForPatient.mockResolvedValue({
      channelId: 'ch-bed7', assessment: null,
    })

    await openChart()
    await waitFor(() => expect(mocks.getLatestAssessmentForPatient).toHaveBeenCalled())

    expectAlertVisible()
    expect(screen.queryByText('AI DRAFT')).not.toBeInTheDocument()
    expect(screen.queryByText(/Sage's assessment of this alert/i)).not.toBeInTheDocument()
    // Not even an empty container: "no draft" is the ordinary state of most
    // charts, and a box that appears on every one of them teaches people to
    // stop reading it.
    expect(document.querySelector('.sage-draft')).toBeNull()
  })

  it('renders the alert when the assessment read fails outright', async () => {
    mocks.getLatestAssessmentForPatient.mockRejectedValue(new Error('boom'))

    await openChart()
    // The failure is reported (this patient IS critical, so a draft was
    // plausible) and the alert is untouched beside it.
    await screen.findByText(/could not check for an assessment/i)
    expectAlertVisible()
  })

  it('places the draft BELOW the alert when one exists', async () => {
    mocks.getLatestAssessmentForPatient.mockResolvedValue({
      channelId: 'ch-bed7',
      assessment: {
        id: 'asm-1', patient_id: 'P007', channel_id: 'ch-bed7', message_id: 4021,
        lane: 'ward', state: 'ready', version: 1,
        unavailable_reason: null, unavailable_message: null, fallback_text: null,
        news2: 19, news2_level: 'critical', news2_drivers: [],
        whats_happening: 'Worsening type 1 respiratory failure.',
        considerations: [{
          key: 'c0', step: 'Escalate now', why: null, proposed_action_type: null,
          proposed_target: null, orb_can_act: false, action_id: null,
          action_type: null, advice_only_reason: null,
        }],
        evidence: [], citation_basis: 'none',
        evidence_note: 'No guideline in Orb\'s corpus matched this alert.',
        watch_items: ['Repeat observations in 15 minutes'],
        confidence: 'low', model: 'medgemma:27b', generated_ms: 46300, ttft_ms: 7400,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        disclaimer: 'Draft decision support. Nothing here has been ordered, charted or '
          + 'administered. Every action requires a clinician\'s explicit approval.',
      },
    })

    await openChart()
    await screen.findByText('AI DRAFT')

    expectAlertVisible()
    const body = document.body.textContent || ''
    // DOM order is the reading order and the screen-reader order: alert, then
    // draft. If the block is ever moved above the risk panel this flips.
    expect(body.indexOf('Respiratory rate 32')).toBeLessThan(body.indexOf('AI DRAFT'))
  })
})
