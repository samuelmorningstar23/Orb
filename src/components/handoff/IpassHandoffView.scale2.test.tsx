/**
 * The I-PASS sheet names SpO2 Scale 2 beside the NEWS2 score.
 *
 * Found 2026-10-02 (Scale 2 lane sweep). A COPD patient scored on Scale 2
 * (target 88–92%) at SpO2 90 on air reads "NEWS2 0 (stable)" on the screen,
 * a few lines above "SpO₂ 90%". Without the scale beside the score, a nurse
 * taking handover reads that pair as a scoring error. The server now sends
 * `illness_severity.spo2_scale`; the marker shows only when it is 2, so every
 * Scale 1 sheet renders exactly as before.
 *
 * Render setup copied from IpassHandoffView.test.tsx.
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getIpassDraft = vi.fn()
const saveIpass = vi.fn()
const prepareReasonedAssessment = vi.fn()

vi.mock('../../api/ipass', () => ({
  getIpassDraft: (...a: unknown[]) => getIpassDraft(...a),
  saveIpass: (...a: unknown[]) => saveIpass(...a),
  prepareReasonedAssessment: (...a: unknown[]) => prepareReasonedAssessment(...a),
}))

import IpassHandoffView from './IpassHandoffView'

beforeEach(() => {
  getIpassDraft.mockReset()
  saveIpass.mockReset()
  prepareReasonedAssessment.mockReset()
  prepareReasonedAssessment.mockResolvedValue({
    status: 'preparing', assessment: null, stale_after_hours: 12,
    reason: null, message: 'Orb Sage is writing a reasoned assessment for this patient.',
  })
})

const MARKER = 'SpO₂ Scale 2 (target 88–92%)'

function draftWithScale(spo2Scale: number | null | undefined) {
  const illness_severity: Record<string, unknown> = {
    severity: 'Stable', news2: 0, news2_level: 'stable',
    scoring_model: 'NEWS2', drivers: ['All vitals within normal range'],
    text: 'Stable: NEWS2 0 (stable band). Key drivers: All vitals within normal range.',
  }
  if (spo2Scale !== undefined) illness_severity.spo2_scale = spo2Scale
  return {
    patient_id: 'P-TEST-S2',
    generated_at: '2026-10-02T09:00:00Z',
    window_hours: 12,
    source_summary: '1 vitals reading',
    plain_text: 'plain text handoff',
    sections: {
      illness_severity,
      patient_summary: {
        name: 'Ravi Menon', mrn: 'MRN-S2', age: 68, sex: 'M', ward: 'W2', bed: '4',
        diagnosis: 'COPD exacerbation', problems: [], allergies: ['NKDA'],
        text: 'Ravi Menon',
      },
      action_list: {
        active_medications: [], open_care_bundles: [], outstanding_items: [],
      },
      situation_awareness: {
        trend: 'insufficient_data',
        trend_display: 'not enough data to call a trend',
        trend_detail: 'no earlier vitals to compare against',
        active_deterioration: false,
        latest_vitals: {
          timestamp: '2026-10-02T08:40:00Z',
          heart_rate: 80, systolic_bp: 125, diastolic_bp: 75,
          spo2: 90, temperature: 37.0, respiratory_rate: 16,
          consciousness: 'Alert',
        },
        readings_count: 1,
        contingencies: ['If new abnormal vitals develop, repeat a full set of observations and reassess.'],
        text: 'Latest vitals',
      },
      synthesis: { read_back_required: true, confirmed: false, text: 'Read back required.' },
      ai_assessment: null,
    },
  }
}

describe('SpO2 Scale 2 beside the NEWS2 score', () => {
  it('shows the scale and its target when the patient is scored on Scale 2', async () => {
    getIpassDraft.mockResolvedValue(draftWithScale(2))
    render(<IpassHandoffView patientId="P-TEST-S2" />)

    const score = await screen.findByText(/^NEWS2 0 /)
    expect(score.textContent).toContain(`· ${MARKER}`)
  })

  it.each([
    ['Scale 1', 1],
    ['no scale sent', null],
    ['an older server that omits the key', undefined],
  ])('shows nothing extra for %s', async (_label, scale) => {
    getIpassDraft.mockResolvedValue(draftWithScale(scale))
    const { container } = render(<IpassHandoffView patientId="P-TEST-S2" />)

    const score = await screen.findByText(/^NEWS2 0 /)
    expect(score.textContent).not.toContain('Scale 2')
    expect(container.textContent).not.toContain('Scale 2')
  })
})
