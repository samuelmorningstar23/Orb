/**
 * The I-PASS sheet with scoring switched off (ORB_SCORING_ENABLED, 6 October
 * 2026). The server then drafts no severity (every illness_severity value
 * null) and no NEWS2 contingency lines. The sheet must show no Illness
 * Severity section, no severity pill, no "NEWS2" and no empty contingency
 * heading. Under the whole Ward Record profile the handover is switched off;
 * HandoffPage.wardRecord.test.tsx covers that page.
 */
import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getIpassDraft = vi.fn()

vi.mock('../../api/ipass', () => ({
  getIpassDraft: (...a: unknown[]) => getIpassDraft(...a),
  saveIpass: vi.fn(),
  prepareReasonedAssessment: vi.fn(async () => ({
    status: 'unavailable', assessment: null, stale_after_hours: 12, reason: null, message: null,
  })),
}))

import IpassHandoffView from './IpassHandoffView'

const DRAFT = {
  patient_id: 'P-1',
  generated_at: '2026-10-06T09:00:00Z',
  window_hours: 12,
  source_summary: '1 vitals reading',
  plain_text: 'plain text handoff',
  sections: {
    illness_severity: {
      severity: null, news2: null, news2_level: null, scoring_model: null,
      spo2_scale: null, drivers: [], text: null,
    },
    patient_summary: {
      name: 'Asha Rao', mrn: 'MRN-1', age: 44, sex: 'F', ward: 'W1', bed: '3',
      diagnosis: 'Pneumonia', problems: [], allergies: [], text: 'Asha Rao',
    },
    action_list: { active_medications: [], open_care_bundles: [], outstanding_items: [] },
    situation_awareness: {
      trend: 'insufficient_data', trend_display: 'not enough data to call a trend',
      trend_detail: 'no earlier vitals to compare against', active_deterioration: false,
      latest_vitals: {
        timestamp: '2026-10-06T08:40:00Z', heart_rate: 135, systolic_bp: 85, diastolic_bp: 50,
        spo2: 88, temperature: 39.5, respiratory_rate: 30, consciousness: 'Alert',
      },
      readings_count: 1, contingencies: [], text: 'Latest vitals…',
    },
    synthesis: { read_back_required: true, confirmed: false, text: 'Read back required.' },
    ai_assessment: null,
  },
}

afterEach(() => { cleanup(); getIpassDraft.mockReset() })

describe('the I-PASS sheet with scoring switched off', () => {
  it('has no severity section, no NEWS2 and no empty contingency heading', async () => {
    getIpassDraft.mockResolvedValue(DRAFT)
    const { container } = render(<IpassHandoffView patientId="P-1" />)
    await screen.findByText(/Read back required/)
    const text = container.textContent ?? ''
    expect(text).not.toMatch(/Illness Severity/i)
    expect(text).not.toMatch(/NEWS2/)
    expect(text).not.toMatch(/Contingency planning/)
    // The readings themselves are still handed over.
    expect(text).toMatch(/SpO₂ 88%/)
  })
})
