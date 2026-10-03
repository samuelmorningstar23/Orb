/**
 * The handoff print is a signed handover document, so the risk box on it has
 * to say the same clinical fact the chart says: the NEWS2 aggregate. Until
 * 2026-10-02 it printed the 0-1 normalised INDEX as a percent ("45% -
 * ELEVATED" for a NEWS2 of 9), which reads as a probability of deterioration
 * it never was, and a patient with no observations at all printed "0% -
 * STABLE", which reads as a clean set.
 *
 * Nothing in the UI opens this print today, so a direct render is the only
 * proof. Assertions are scoped to the risk box on purpose: the SpO2 cell in
 * the vitals row carries a genuine "%" unit and must keep it.
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ComponentProps } from 'react'
import { HandoffPrintView } from './HandoffPrintView'

type Props = ComponentProps<typeof HandoffPrintView>

const patient: Props['patient'] = {
  mrn: 'MRN-0042',
  name: 'Anita Rao',
  age: 64,
  sex: 'F',
  ward: 'General Medicine',
  bed: 'GM-04',
  diagnosis: 'Community-acquired pneumonia',
}

const latestVitals: Props['latestVitals'] = {
  heart_rate: 112,
  spo2: 93,
  systolic_bp: 98,
  diastolic_bp: 60,
  respiratory_rate: 24,
  temperature: 38.6,
}

const COVERAGE_NOTE =
  'INCOMPLETE observation set: Consciousness (ACVPU), Supplemental O2 not measured. ' +
  'An unmeasured parameter scores 0, so this NEWS2 is a MINIMUM and the true score can only be higher.'

function renderWith(risk: Props['risk']) {
  const { container } = render(
    <HandoffPrintView
      patient={patient}
      risk={risk}
      latestVitals={latestVitals}
      notes={[]}
      medications={[]}
    />,
  )
  const box = container.querySelector('.handoff-risk-box')
  const val = container.querySelector('.handoff-risk-val')
  return { container, box, val }
}

/* The SpO2 unit is the one genuine "%" on the page. The box-scoped checks
   cannot see the index reprinted somewhere else (a header line, the footer),
   so this counts every "%" on the page and requires that the only one sits in
   the SpO2 cell. Not a check for "45": the generated timestamp in the header
   can contain it. */
function expectOnlyPercentIsSpO2(container: HTMLElement) {
  const spo2Cell = Array.from(container.querySelectorAll('.handoff-vital')).find((el) =>
    el.textContent!.includes('SpO'),
  )
  expect(spo2Cell).toBeDefined()
  expect(spo2Cell!.textContent).toContain('%')
  expect(container.textContent!.match(/%/g) ?? []).toHaveLength(1)
}

describe('HandoffPrintView risk box prints NEWS2, never the normalised index', () => {
  it('prints the NEWS2 aggregate and band, not the 0-1 index as a percent', () => {
    const risk = {
      score: 0.45,
      level: 'elevated',
      raw_news2: 9,
      complete: true,
      coverage_note: null,
      single_param_3: false,
      spo2_scale: 1,
    }
    const { container, box, val } = renderWith(risk)

    // Control: the box renders, and the page still has the genuine SpO2 "%"
    // outside it, so the scoped checks below are not trivially true.
    expect(box).not.toBeNull()
    expect(val).not.toBeNull()
    expect(container.querySelector('.handoff-vitals-row')!.textContent).toContain('%')

    expect(val!.textContent).not.toContain('%')
    expect(val!.textContent).toContain('NEWS2 9')
    expect(val!.textContent).toContain('elevated')
    // The index must not leak in any form: not as 45, not as 0.45.
    expect(box!.textContent).not.toMatch(/45/)
    // Nor anywhere else on the page.
    expectOnlyPercentIsSpO2(container)
  })

  it('heads the box NEWS2 rather than calling the index a risk score', () => {
    const { box } = renderWith({ score: 0.45, level: 'elevated', raw_news2: 9, complete: true })
    expect(box).not.toBeNull()
    expect(box!.querySelector('h3')!.textContent).toBe('NEWS2')
    expect(box!.textContent).not.toMatch(/risk score/i)
  })

  /* The exact shape vigil_service returns for a patient with no vitals. It
     has no raw_news2, and score 0 is "nothing measured", not "zero points". */
  it('says NEWS2 not recorded for the no-vitals shape instead of 0%', () => {
    const noVitals = {
      score: 0,
      level: 'stable',
      drivers: ['No vitals recorded'],
      scoring_model: 'NEWS2',
    }
    const { container, box, val } = renderWith(noVitals)
    expect(box).not.toBeNull()

    for (const bad of [/%/, /undefined/, /NaN/]) {
      expect(box!.textContent).not.toMatch(bad)
    }
    expect(val!.textContent).toBe('NEWS2 not recorded')
    expectOnlyPercentIsSpO2(container)
  })

  /* The other side of the no-vitals case: a full set of normal observations
     really does score 0, and the paper must say 0. A falsy check on raw_news2
     would print this patient as "not recorded", which reads as nobody having
     taken the observations at all. */
  it('prints a measured, complete NEWS2 of 0 as 0, not as not recorded', () => {
    const { container, box, val } = renderWith({
      score: 0,
      level: 'stable',
      raw_news2: 0,
      complete: true,
      coverage_note: null,
    })
    expect(box).not.toBeNull()
    expect(val!.textContent).toBe('NEWS2 0 (stable)')
    expect(box!.textContent).not.toMatch(/%/)
    expectOnlyPercentIsSpO2(container)
  })

  it.each([
    ['null', null],
    ['NaN', Number.NaN],
    ['Infinity', Number.POSITIVE_INFINITY],
  ])('treats a raw_news2 of %s as not recorded', (_label, raw) => {
    const { box, val } = renderWith({ score: 0, level: 'stable', raw_news2: raw })
    expect(box).not.toBeNull()
    expect(box!.textContent).not.toMatch(/%|undefined|NaN|Infinity|null/)
    expect(val!.textContent).toBe('NEWS2 not recorded')
  })

  /* An incomplete set is a minimum, not a score. The server already wrote
     the sentence that says so; the paper must carry it, word for word. */
  it('prints the server coverage note verbatim when the set is incomplete', () => {
    const { container, box, val } = renderWith({
      score: 0.25,
      level: 'elevated',
      raw_news2: 5,
      complete: false,
      coverage_note: COVERAGE_NOTE,
    })
    expect(box).not.toBeNull()
    expect(box!.textContent).toContain(COVERAGE_NOTE)
    // This is the only fixture where the note renders, so it is the only one
    // that can see the index sneaking back in on the incomplete path alone,
    // in the value or inside the note. Index 0.25; the note has no "25" or "%".
    expect(box!.textContent).not.toMatch(/%|25/)
    // The aggregate is still printed; the note qualifies it, it does not replace it.
    // "at least" is allowed: the ward-board session is moving news2Description to
    // say an incomplete set is a minimum, and this sheet takes its wording.
    expect(val!.textContent).toMatch(/^NEWS2 (at least )?5\b/)
    expectOnlyPercentIsSpO2(container)
  })

  it('prints no coverage note when the set is complete or the note is empty', () => {
    const complete = renderWith({
      score: 0.45, level: 'elevated', raw_news2: 9, complete: true, coverage_note: COVERAGE_NOTE,
    })
    expect(complete.box!.textContent).not.toContain('INCOMPLETE')

    const empty = renderWith({
      score: 0.25, level: 'elevated', raw_news2: 5, complete: false, coverage_note: '',
    })
    expect(empty.box!.textContent).not.toMatch(/INCOMPLETE|null|undefined/)

    const missing = renderWith({
      score: 0.25, level: 'elevated', raw_news2: 5, complete: false, coverage_note: null,
    })
    expect(missing.box!.textContent).not.toMatch(/INCOMPLETE|null|undefined/)

    // A whitespace-only note has no text to check for, so check the element:
    // an empty note line is a blank the reader is left to fill in.
    const blank = renderWith({
      score: 0.25, level: 'elevated', raw_news2: 5, complete: false, coverage_note: '   ',
    })
    for (const r of [complete, empty, missing, blank]) {
      expect(r.box!.querySelector('.handoff-risk-note')).toBeNull()
    }
  })

  /* A patient can sit at an aggregate of 3 and still need urgent review. The
     printed sentence must say why, the same way the badge does. */
  it('carries the single-parameter-3 and SpO2 Scale 2 notes from news2Description', () => {
    const { container, box, val } = renderWith({
      score: 0.15,
      level: 'elevated',
      raw_news2: 3,
      complete: true,
      single_param_3: true,
      spo2_scale: 2,
    })
    expect(val).not.toBeNull()
    expect(val!.textContent).toContain('a single parameter scores 3')
    expect(val!.textContent).toContain('SpO₂ Scale 2')
    expect(val!.textContent).toContain('NEWS2 3')
    // Index 0.15: neither "15" nor "%" belongs anywhere in this box.
    expect(box!.textContent).not.toMatch(/%|15/)
    expectOnlyPercentIsSpO2(container)
  })
})
