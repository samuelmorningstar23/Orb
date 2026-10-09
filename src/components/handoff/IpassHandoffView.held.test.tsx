/**
 * A medicine on hold on the I-PASS sheet (medorders-6b, W7-E).
 *
 * A held medication order keeps its ward row active, so the server lists it
 * among the active medications with on_hold, held_at and hold_reason. The
 * sheet must say it is on hold, with the hold time through the shared clinical
 * date formatter, or a nurse taking handover reads a held drug as being given.
 * A drug that is not held reads exactly as before.
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
import { formatClinicalDateTime } from '../../lib/datetime'

const HELD_AT = '2026-10-09T08:50:00Z'

function draft(activeMedications: unknown[]) {
  return {
    patient_id: 'P-HELD-1',
    generated_at: '2026-10-09T09:00:00Z',
    window_hours: 12,
    source_summary: '1 vitals reading',
    plain_text: 'plain text handoff',
    sections: {
      illness_severity: {
        severity: 'Stable', news2: 1, news2_level: 'low',
        scoring_model: 'NEWS2', drivers: [], text: 'Stable.',
      },
      patient_summary: {
        name: 'Asha Rao', mrn: 'MRN-1', age: 44, sex: 'F', ward: 'W1', bed: '3',
        diagnosis: 'Pneumonia', problems: [], allergies: ['None known'], text: 'Asha Rao',
      },
      action_list: {
        active_medications: activeMedications, open_care_bundles: [], outstanding_items: [],
      },
      situation_awareness: {
        trend: 'stable', trend_display: 'stable', trend_detail: '',
        active_deterioration: false,
        latest_vitals: {
          timestamp: '2026-10-09T08:40:00Z',
          heart_rate: 80, systolic_bp: 120, diastolic_bp: 70,
          spo2: 97, temperature: 36.8, respiratory_rate: 16, consciousness: 'Alert',
        },
        readings_count: 1, contingencies: [], text: '',
      },
      synthesis: { read_back_required: true, confirmed: false, text: 'Read back required.' },
      ai_assessment: null,
    },
  }
}

beforeEach(() => {
  getIpassDraft.mockReset()
  saveIpass.mockReset()
  prepareReasonedAssessment.mockReset()
  prepareReasonedAssessment.mockResolvedValue({
    status: 'preparing', assessment: null, stale_after_hours: 12,
    reason: null, message: 'Orb Sage is writing a reasoned assessment for this patient.',
  })
})

describe('a held medicine on the handover sheet', () => {
  it('says it is on hold, since when and why, and leaves other drugs as they were', async () => {
    getIpassDraft.mockResolvedValue(draft([
      {
        drug_name: 'Furosemide', dosage: '40 mg', route: 'IV', frequency: 'OD',
        unverified: false, on_hold: true, held_at: HELD_AT,
        hold_reason: 'Nil by mouth before theatre',
        hold_label: 'on hold since 9 Oct 2026, 14:20 IST: Nil by mouth before theatre',
      },
      { drug_name: 'Paracetamol', dosage: '1 g', route: 'Oral', frequency: 'QDS', unverified: false },
    ]))
    render(<IpassHandoffView patientId="P-HELD-1" />)

    const held = (await screen.findByText(/Furosemide/)).closest('li')!
    expect(held.textContent).toContain(
      `(on hold since ${formatClinicalDateTime(HELD_AT)}: Nil by mouth before theatre)`)
    const plain = screen.getByText(/Paracetamol/).closest('li')!
    expect(plain.textContent).not.toMatch(/on hold/i)
  })

  it('still says on hold when no time or reason came with it', async () => {
    getIpassDraft.mockResolvedValue(draft([
      {
        drug_name: 'Heparin', dosage: '5000 units', route: 'SC', frequency: 'BD',
        on_hold: true, held_at: null, hold_reason: null,
      },
    ]))
    render(<IpassHandoffView patientId="P-HELD-1" />)

    const held = (await screen.findByText(/Heparin/)).closest('li')!
    expect(held.textContent).toContain('(on hold)')
  })
})
