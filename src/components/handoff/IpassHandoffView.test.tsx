/**
 * The I-PASS handoff view receives the same deliberately ambiguous 404 the
 * downtime page does — the server answers an unknown patient id and an
 * out-of-department one identically so a refusal never confirms the record
 * exists. This view printed the body straight ("Could not draft handoff:
 * Patient not found"), which both tells a clinician the patient does not exist
 * and undoes the ambiguity the API was built to preserve.
 *
 * Pinned here: this surface uses the SHARED wording (lib/scopeRefusal), and a
 * real failure still reaches the clinician as itself.
 */
import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
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
import { refusalMessage } from '../../lib/scopeRefusal'

beforeEach(() => {
  getIpassDraft.mockReset()
  saveIpass.mockReset()
  prepareReasonedAssessment.mockReset()
  prepareReasonedAssessment.mockResolvedValue({
    status: 'preparing', assessment: null, stale_after_hours: 12,
    reason: null, message: 'Orb Sage is writing a reasoned assessment for this patient.',
  })
})

describe('scope refusal wording', () => {
  it('shows the shared careful refusal, not the raw "Patient not found" body', async () => {
    getIpassDraft.mockRejectedValue(new Error('Patient not found'))
    render(<IpassHandoffView patientId="P-TEST-1" />)

    const shown = await screen.findByText(refusalMessage('Patient not found')!)
    expect(shown).toBeInTheDocument()
    expect(screen.queryByText(/Could not draft handoff/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Patient not found/)).not.toBeInTheDocument()
  })

  it('leaves a genuine failure worded as itself', async () => {
    getIpassDraft.mockRejectedValue(new Error('Sage is unavailable'))
    render(<IpassHandoffView patientId="P-TEST-1" />)

    expect(await screen.findByText(/Could not draft handoff: Sage is unavailable/i)).toBeInTheDocument()
    expect(screen.queryByText(/not available to you/i)).not.toBeInTheDocument()
  })
})

/* ──────────────────────────────────────────────────────────────────────────
 * THE SHEET IS READ AT A BEDSIDE AND PRINTED. It may not speak machine.
 *
 * Two defects, both of them small and both of them the kind a clinician
 * silently loses trust over:
 *
 *   · the vitals trend printed the raw wire enum — a nurse taking handover was
 *     shown "Trend over shift: insufficient_data" when what the sheet meant was
 *     "there is not enough data to call a trend". The server already carries
 *     the clinical phrasing alongside the enum; the enum is for matching on;
 *   · every vital printed at whatever precision the float arrived with, so a
 *     stored 110.8 became "BP 110.8/62.3". Blood pressure is read off a cuff to
 *     the nearest whole mmHg — the decimal claims an instrument nobody used.
 *
 * These assert the PRODUCED TEXT, not the props: what a reader would see.
 * ────────────────────────────────────────────────────────────────────────── */

const DRAFT_WITH_NO_TREND = {
  patient_id: 'P-TEST-1',
  generated_at: '2026-08-14T09:00:00Z',
  window_hours: 12,
  source_summary: '1 vitals reading',
  plain_text: 'plain text handoff',
  sections: {
    illness_severity: {
      severity: 'Watcher', news2: 3, news2_level: 'low_risk',
      scoring_model: 'NEWS2', drivers: ['SpO2 93%'],
      text: 'Watcher — NEWS2 3.',
    },
    patient_summary: {
      name: 'Asha Rao', mrn: 'MRN-1', age: 44, sex: 'F', ward: 'W1', bed: '3',
      diagnosis: 'Pneumonia', problems: [], allergies: ['None known'],
      text: 'Asha Rao',
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
        timestamp: '2026-08-14T08:40:00Z',
        heart_rate: 88.4, systolic_bp: 110.8, diastolic_bp: 62.3,
        spo2: 93.6, temperature: 37.44, respiratory_rate: 19.5,
        consciousness: 'Alert',
      },
      readings_count: 1,
      contingencies: ['If SpO2 falls below 92%, start oxygen.'],
      text: 'Latest vitals…',
    },
    synthesis: { read_back_required: true, confirmed: false, text: 'Read back required.' },
    // ALWAYS PRESENT AS A KEY on the wire, and null far more often than not.
    ai_assessment: null,
  },
}

/** The server's own constants — `ipass_service.AI_SUPPLEMENT_HEADING`,
 *  `AI_DRAFTED_LABEL` and `AI_SUPPLEMENT_DISCLAIMER`, character for character.
 *  Copied here rather than paraphrased BECAUSE the assertion is that the client
 *  does not paraphrase: a test written against reworded text would pass while
 *  the medico-legal labelling drifted. */
const HEADING = 'SUPPLEMENT — AI-DRAFTED ASSESSMENT (NOT WRITTEN BY A CLINICIAN)'
const LABEL = 'AI-drafted by Orb Sage, not written by a clinician'
const DISCLAIMER =
  'Nothing in this section has been ordered, charted or administered. ' +
  'It is machine-generated decision support and carries no clinical authority.'

const READY_ASSESSMENT = {
  available: true as const,
  ai_drafted: true as const,
  authored_by: 'Orb Sage (artificial intelligence)',
  label: LABEL,
  heading: HEADING,
  disclaimer: DISCLAIMER,
  assessment_id: 'assess-1',
  reused: true,
  reuse_basis: 'drafted_for_vigil_alert',
  reuse_basis_display: 'when Vigil paged for this patient',
  drafted_at: '2026-08-14T08:12:00+00:00',
  drafted_at_display: '14 Aug, 08:12',
  age_minutes: 48,
  age_display: '48 minutes ago',
  stale_after_hours: 12,
  state: 'ready',
  state_display: 'not yet reviewed by a clinician',
  endorsed_by: null,
  endorsed_at: null,
  news2_at_drafting: 9,
  news2_level_at_drafting: 'critical',
  news2_context: 'NEWS2 9 (critical) as scored when this was written',
  whats_happening: 'Rising respiratory rate with a falling SpO2 on oxygen.',
  considerations: [
    { step: 'Repeat arterial blood gas', why: 'Type 2 failure not excluded', orb_can_act: false },
    { step: 'Escalate to the medical registrar', why: 'Critical band', orb_can_act: true },
  ],
  watch_items: ['Whether the patient looks exhausted', 'Whether there is new confusion'],
  evidence: [{ title: 'Sepsis Six — local policy', source: 'Hospital policy' }],
  citation_basis: 'local_guideline',
  citation_display: 'a local hospital guideline',
  confidence: 'moderate',
  confidence_display: 'moderate',
  model: 'medgemma:27b',
}

/* ──────────────────────────────────────────────────────────────────────────
 * D3 — TIER 3 WAS SERVED AND NEVER SHOWN.
 *
 * `GET /api/ipass/{id}/reasoned-assessment` returned 200 with available:true,
 * state:ready, the heading, four considerations and four watch items — and this
 * page contained the string "Sage" ZERO times. `grep -rn reasoned src/` returned
 * nothing. Relocating the 46-second assessment off the alert and onto the
 * handover was the entire point of the tier split, and it had been relocated to
 * nowhere.
 *
 * Two properties, and the second is as important as the first:
 *   1. a ready assessment is SHOWN, with the server's labelling verbatim;
 *   2. no assessment shows NOTHING — not a heading over an empty box.
 * ────────────────────────────────────────────────────────────────────────── */
describe('the AI-drafted assessment on the handover sheet', () => {
  const withAssessment = {
    ...DRAFT_WITH_NO_TREND,
    sections: { ...DRAFT_WITH_NO_TREND.sections, ai_assessment: READY_ASSESSMENT },
  }

  it('shows a ready assessment, with the server\'s heading and disclaimer VERBATIM', async () => {
    getIpassDraft.mockResolvedValue(withAssessment)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(HEADING)
    const shown = container.textContent!
    // The labelling, character for character. Not "contains the word AI".
    expect(shown).toContain(HEADING)
    expect(shown).toContain(LABEL)
    expect(shown).toContain(DISCLAIMER)
    // And the assessment itself.
    expect(shown).toContain('Rising respiratory rate with a falling SpO2 on oxygen.')
    expect(shown).toContain('Repeat arterial blood gas')
    expect(shown).toContain('Whether there is new confusion')
    expect(shown).toContain('Sepsis Six — local policy')
  })

  it('stamps it as a snapshot and names its author, so it cannot read as current', async () => {
    getIpassDraft.mockResolvedValue(withAssessment)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(HEADING)
    const shown = container.textContent!
    expect(shown).toContain('Orb Sage (artificial intelligence)')
    expect(shown).toContain('48 minutes ago')
    expect(shown).toContain('NEWS2 9 (critical) as scored when this was written')
    expect(shown).toContain('it has not been re-run')
    // Nobody has read it, and the sheet says so rather than implying approval.
    expect(shown).toContain('not yet reviewed by a clinician')
  })

  it('prints the server\'s phrasing, never a raw enum, inside the supplement', async () => {
    getIpassDraft.mockResolvedValue(withAssessment)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(HEADING)
    const section = container.querySelector('.orb-assess')!
    expect(section.textContent).toContain('when Vigil paged for this patient')
    expect(section.textContent).not.toContain('drafted_for_vigil_alert')
    expect(section.textContent).not.toContain('local_guideline')
    expect(section.textContent).not.toMatch(/[a-z]+_[a-z]+/)
  })

  it('renders NOTHING — not an empty heading — when there is no assessment', async () => {
    getIpassDraft.mockResolvedValue(DRAFT_WITH_NO_TREND)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(/Trend over shift/)
    expect(container.querySelector('.orb-assess')).toBeNull()
    const rendered = container.textContent!.split('Plain-text handoff')[0]
    expect(rendered).not.toContain('SUPPLEMENT')
    expect(rendered).not.toContain('AI-drafted')
    expect(rendered).not.toMatch(/no assessment|unavailable|Sage is writing/i)
  })

  it('asks Sage for one when the sheet is opened — the sheet itself never generates', async () => {
    getIpassDraft.mockResolvedValue(DRAFT_WITH_NO_TREND)
    render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(/Trend over shift/)
    // ASK EARLY, READ LATER: the handover is composed minutes ahead of the
    // change-over, so the ~46 s is spent against a clinician already reading.
    expect(prepareReasonedAssessment).toHaveBeenCalledWith('P-TEST-1')
  })

  it('does not spend GPU asking again for a patient who already has one', async () => {
    getIpassDraft.mockResolvedValue(withAssessment)
    render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(HEADING)
    // One ask per open, never one per render.
    expect(prepareReasonedAssessment).toHaveBeenCalledTimes(1)
  })

  it('a failed prepare is silent — it never displaces the handover', async () => {
    // Preparing can be refused for permission (`sage.query` is a separate grant
    // from reading the chart), for load, or because the feature is off. None of
    // those is news to someone taking handover.
    prepareReasonedAssessment.mockRejectedValue(new Error('Not authorized'))
    getIpassDraft.mockResolvedValue(DRAFT_WITH_NO_TREND)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(/Trend over shift/)
    expect(container.textContent).not.toMatch(/Not authorized/i)
    expect(container.textContent).toContain('I-PASS Shift Handoff')
  })
})

describe('nothing machine-shaped reaches the reader', () => {
  it('says why there is no trend instead of printing "insufficient_data"', async () => {
    getIpassDraft.mockResolvedValue(DRAFT_WITH_NO_TREND)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(/Trend over shift/)
    expect(container.textContent).toContain('not enough data to call a trend')
    expect(container.textContent).not.toContain('insufficient_data')
  })

  it('falls back to humanising the enum when the server sends no phrasing', async () => {
    const { trend_display: _dropped, ...noDisplay } = DRAFT_WITH_NO_TREND.sections.situation_awareness
    getIpassDraft.mockResolvedValue({
      ...DRAFT_WITH_NO_TREND,
      sections: { ...DRAFT_WITH_NO_TREND.sections, situation_awareness: noDisplay },
    })
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(/Trend over shift/)
    expect(container.textContent).toContain('Insufficient data')
    expect(container.textContent).not.toContain('insufficient_data')
  })

  it('leaves no snake_case token anywhere in the rendered sheet', async () => {
    getIpassDraft.mockResolvedValue(DRAFT_WITH_NO_TREND)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    await screen.findByText(/Trend over shift/)
    // The <details> holds the server's own plain-text handoff verbatim; the
    // rendered sections are what this asserts.
    const rendered = container.textContent!.split('Plain-text handoff')[0]
    expect(rendered).not.toMatch(/[a-z]+_[a-z]+/)
    // ("None known" / "None charted" are English and stay — the Python `None`
    //  this sweep was chasing lives in the Scribe sign gate, pinned there.)
  })
})

describe('vitals on a sheet that gets printed', () => {
  it('renders blood pressure in whole mmHg, never to a tenth', async () => {
    getIpassDraft.mockResolvedValue(DRAFT_WITH_NO_TREND)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)

    const line = await screen.findByText(/^Latest: /)
    expect(line.textContent).toContain('BP 111/62')
    expect(line.textContent).not.toContain('110.8')
    expect(line.textContent).not.toContain('62.3')
    expect(container.textContent).not.toContain('110.8')
  })

  it('rounds pulse, respiratory rate and SpO2 too, and keeps temperature to a tenth', async () => {
    getIpassDraft.mockResolvedValue(DRAFT_WITH_NO_TREND)
    render(<IpassHandoffView patientId="P-TEST-1" />)

    const line = await screen.findByText(/^Latest: /)
    expect(line.textContent).toContain('HR 88')
    expect(line.textContent).toContain('RR 20')
    expect(line.textContent).toContain('SpO₂ 94%')
    // Temperature IS charted to a tenth — it must not be rounded away.
    expect(line.textContent).toContain('Temp 37.4°C')
  })

  it('prints a dash for a vital that was never recorded, not 0', async () => {
    getIpassDraft.mockResolvedValue({
      ...DRAFT_WITH_NO_TREND,
      sections: {
        ...DRAFT_WITH_NO_TREND.sections,
        situation_awareness: {
          ...DRAFT_WITH_NO_TREND.sections.situation_awareness,
          latest_vitals: {
            ...DRAFT_WITH_NO_TREND.sections.situation_awareness.latest_vitals,
            systolic_bp: null, diastolic_bp: null,
          },
        },
      },
    })
    render(<IpassHandoffView patientId="P-TEST-1" />)

    const line = await screen.findByText(/^Latest: /)
    expect(line.textContent).toContain('BP --/--')
    expect(line.textContent).not.toContain('BP 0/0')
  })
})

/* ── Print ───────────────────────────────────────────────────────────────────
 * src/print.css hides every element at print time except the named print
 * layouts. The sheet's root carried none of them, so its Print button produced
 * a blank page. Pinned from both ends: the root carries the reveal class, and
 * print.css reveals it and keeps the on-screen chrome off the paper. */
describe('print', () => {
  it('renders the sheet under the print reveal root, with its chrome marked', async () => {
    getIpassDraft.mockResolvedValue(DRAFT_WITH_NO_TREND)
    const { container } = render(<IpassHandoffView patientId="P-TEST-1" />)
    await screen.findByText('I-PASS Shift Handoff')

    const root = container.querySelector('.ipass-print-layout')
    expect(root).not.toBeNull()
    const printBtn = screen.getByRole('button', { name: 'Print' })
    expect(printBtn.closest('.ipass-actions')).not.toBeNull()
    expect(root!.querySelector('.ipass-plain-text')).not.toBeNull()
  })

  it('print.css reveals the I-PASS and bill layouts and hides their chrome', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/print.css'), 'utf8')
    const media = css.slice(css.indexOf('@media print'))
    for (const sel of ['.ipass-print-layout *', '.bill-print-layout *']) {
      expect(media).toContain(sel)
    }
    for (const sel of ['.ipass-print-layout .ipass-actions',
                       '.ipass-print-layout .ipass-plain-text',
                       '.bill-print-layout .bill-actions']) {
      expect(media).toContain(sel)
    }
  })
})
