/**
 * The three round columns must survive an AI supplement.
 *
 * With the assessment rendered open as the last child of a clipped flex column,
 * the columns shrank to 0px at 1440x900: "what changed", "current issues" and
 * "decide today" vanished on exactly the patients sick enough to have a
 * supplement. jsdom computes no layout, so the DOM half is asserted here (all
 * three headings present, the supplement collapsed, its heading verbatim on the
 * summary line) and the layout half is asserted where it lives, in Round.css.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import RoundSummaryView from './RoundSummaryView'
import type { RoundPatient } from './roundQueue'
import type { IpassReasonedAssessment } from '../../api/ipass'

const HEADING = 'SUPPLEMENT: AI-DRAFTED ASSESSMENT (NOT WRITTEN BY A CLINICIAN)'

const PATIENT: RoundPatient = {
  id: 'P002', name: 'Anita Rao', bed: 'GM-102', ward: 'GM', mrn: 'MRN-P002',
  age: 62, sex: 'F', diagnosis: 'Community-acquired pneumonia',
  admission_date: '2026-08-05', risk_score: 7, risk_level: 'critical',
  risk_drivers: [], attending_doctor_name: 'Dr Iyer',
}

const ASSESSMENT = {
  available: true,
  ai_drafted: true,
  authored_by: 'Orb Sage (artificial intelligence)',
  label: 'AI-drafted by Orb Sage, not written by a clinician',
  heading: HEADING,
  disclaimer: 'It is machine-generated decision support and carries no clinical authority.',
  assessment_id: 'assess-1',
  reused: true,
  reuse_basis: 'drafted_for_review',
  reuse_basis_display: 'when this patient was prepared for review',
  drafted_at: '2026-08-07T07:40:00+00:00',
  drafted_at_display: '7 Aug, 07:40',
  age_minutes: 20,
  age_display: '20 minutes ago',
  stale_after_hours: 12,
  state: 'ready',
  state_display: 'not yet reviewed by a clinician',
  endorsed_by: null,
  endorsed_at: null,
  news2_at_drafting: 7,
  news2_level_at_drafting: 'critical',
  news2_context: 'NEWS2 7 (critical) as scored when this was written',
  whats_happening: 'Persistent tachypnoea despite antibiotics.',
  considerations: [],
  watch_items: [],
  evidence: [],
  citation_basis: 'no_guideline',
  citation_display: 'no guideline in Orb’s corpus',
  confidence: 'moderate',
  confidence_display: 'moderate',
  model: 'medgemma:27b',
} as unknown as IpassReasonedAssessment

function renderSummary(assessment: IpassReasonedAssessment | null) {
  return render(
    <RoundSummaryView
      patient={PATIENT}
      card={null}
      storyPhase="ready"
      story={[]}
      storyError={null}
      onRetryStory={() => {}}
      insights={[]}
      insightsFailed={false}
      hasPlanToday={false}
      assessment={assessment}
      now={new Date(2026, 7, 7, 9, 0, 0)}
    />,
  )
}

afterEach(cleanup)

describe('RoundSummaryView with an AI supplement', () => {
  it('keeps all three column headings and collapses the supplement under them', () => {
    const { container } = renderSummary(ASSESSMENT)

    expect(container.querySelector('#round-col-changed')?.textContent).toMatch(/^Since /)
    expect(screen.getByRole('heading', { name: 'Current issues' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Decide today' })).toBeTruthy()

    const details = container.querySelector('details.round-assess') as HTMLDetailsElement
    expect(details).not.toBeNull()
    expect(details.open).toBe(false)
    // The labelling is on screen before the block is opened, word for word.
    expect(details.querySelector('summary')?.textContent).toBe(HEADING)
    // The block itself is inside the wrapper, after the columns.
    expect(details.querySelector('.orb-assess')).not.toBeNull()
    const cols = container.querySelector('.round-cols')!
    expect(cols.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('renders no wrapper and no summary line when there is no assessment', () => {
    const { container } = renderSummary(null)
    expect(container.querySelector('.round-assess')).toBeNull()
    expect(container.textContent).not.toContain('SUPPLEMENT')
  })

  it('renders no wrapper around an assessment with nothing to say', () => {
    const { container } = renderSummary({ ...ASSESSMENT, whats_happening: '   ' })
    expect(container.querySelector('.round-assess')).toBeNull()
  })
})

describe('Round.css keeps the columns on screen', () => {
  const css = readFileSync(resolve(process.cwd(), 'src/components/round/Round.css'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
  const rule = (selector: string) => {
    const re = new RegExp(`(^|\\})\\s*${selector.replace(/[.]/g, '\\.')}\\s*\\{([^}]*)\\}`, 'g')
    return [...css.matchAll(re)].map(m => m[2]).join(';')
  }

  it('the summary scrolls instead of clipping', () => {
    const body = rule('.round-summary')
    expect(body).toMatch(/overflow-y:\s*auto/)
    expect(body).not.toMatch(/overflow:\s*hidden/)
  })

  it('the three columns have a height floor', () => {
    expect(rule('.round-cols')).toMatch(/min-height:\s*160px/)
  })
})
