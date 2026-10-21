/**
 * The handover sheet carries the nurses' shift notes of the window (21 October
 * 2026): under Situation Awareness, each with the ward-clock time, the shift
 * and the author as the server words them, and no heading at all when the
 * draft carries none or comes from an older server without the key.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getIpassDraft = vi.fn()

vi.mock('../../api/ipass', () => ({
  getIpassDraft: (...a: unknown[]) => getIpassDraft(...a),
  prepareReasonedAssessment: vi.fn(async () => ({ status: 'unavailable', assessment: null, stale_after_hours: 12, reason: null, message: null })),
  saveIpass: vi.fn(),
}))

import IpassHandoffView from './IpassHandoffView'

function draft(nursingNotes?: unknown[]) {
  const sections: Record<string, unknown> = {
    illness_severity: { severity: null, news2: null, news2_level: null, scoring_model: null, drivers: [], text: null },
    patient_summary: { name: 'Meena Pillai', mrn: 'MRN-31', age: 47, sex: 'F', ward: 'GM-1', bed: '31',
      diagnosis: 'Post-operative day 2', problems: [], allergies: [], text: 'Meena Pillai, 47F.' },
    action_list: { active_medications: [], open_care_bundles: [], outstanding_items: [] },
    situation_awareness: { trend: 'stable', trend_detail: 'no change', active_deterioration: false,
      latest_vitals: { timestamp: null, heart_rate: 80, systolic_bp: 120, diastolic_bp: 70, spo2: 97,
        temperature: 36.8, respiratory_rate: 16, consciousness: 'alert' },
      readings_count: 2, contingencies: [], text: 'Stable.' },
    synthesis: { read_back_required: true, confirmed: false, text: 'Read back.' },
    ai_assessment: null,
  }
  if (nursingNotes) sections.nursing_notes = nursingNotes
  return { patient_id: 'P77', generated_at: '2026-10-21T06:00:00+00:00', window_hours: 12,
    source_summary: 'drafted', sections, plain_text: 'I-PASS SHIFT HANDOFF' }
}

beforeEach(() => { getIpassDraft.mockReset() })
afterEach(() => cleanup())

describe('nursing notes on the handover', () => {
  it('lists each note with the server\'s time, shift and author words', async () => {
    getIpassDraft.mockResolvedValue(draft([
      { id: 5, written_at: '2026-10-21T04:20:00+00:00', written_at_display: '21 Oct 2026, 09:50 IST',
        shift_label: '07:00 to 15:00', author_id: 'NUR001', author_name: 'Sister Mary Thomas',
        content: 'Settled after analgesia.' },
    ]))
    render(<IpassHandoffView patientId="P77" />)
    const block = await screen.findByTestId('ipass-nursing-notes')
    expect(block.textContent).toContain('Nursing notes (last 12h)')
    expect(block.textContent).toContain('21 Oct 2026, 09:50 IST, 07:00 to 15:00, Sister Mary Thomas:')
    expect(block.textContent).toContain('Settled after analgesia.')
  })

  it('prints no heading when the window holds no notes, or the server sends no key', async () => {
    getIpassDraft.mockResolvedValue(draft([]))
    render(<IpassHandoffView patientId="P77" />)
    await screen.findByText('Read back.')
    expect(screen.queryByTestId('ipass-nursing-notes')).toBeNull()
    cleanup()
    getIpassDraft.mockResolvedValue(draft(undefined))
    render(<IpassHandoffView patientId="P77" />)
    await screen.findByText('Read back.')
    expect(screen.queryByTestId('ipass-nursing-notes')).toBeNull()
  })
})
