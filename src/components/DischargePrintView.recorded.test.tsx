/**
 * The printed discharge summary carries no clinical statement that was not
 * recorded. Until 2026-10-03 it printed the same history, examination, course,
 * "FOR 5 DAYS" and default "Oral" on every drug, an invented IP number, a named
 * fallback signatory and a follow-up interval for every patient, and it reached
 * patients through the discharge link.
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DischargePrintView } from './DischargePrintView'

const TEMPLATE = [
  /No H\/o/i, /Not specific/i, /Conservatively/i, /responded well/i, /hemodynamically/i,
  /FOR 5 DAYS/i, /IP\/08\//, /Vikram Rathore/, /Follow up in OPD/i, /S1,\s*S2/, /Afebrile/i,
  /Bilateral Air Entry/i, /Non-tender/i, /Sleep: Adequate/i, /DOS:/,
]

const BARE = {
  name: 'Ravi Kumar', mrn: 'MRN-0021', age: 58, sex: 'M', ward: 'GM-2', bed: '21',
  diagnosis: null, admission_date: null, attending_doctor_name: null, admission_id: null,
}

describe('a patient with nothing documented', () => {
  it('prints none of the old template and says what is not recorded', () => {
    const { container } = render(
      <DischargePrintView patient={BARE} medications={[]} allergies={[]} />,
    )
    const text = container.textContent || ''
    for (const phrase of TEMPLATE) expect(text).not.toMatch(phrase)
    expect(text).toContain('MRN-0021')
    expect(text).toMatch(/Not documented/)
    expect(text).toMatch(/Not recorded/)
    expect(text).toContain('No discharge medicines recorded')
    expect(text).toContain('No allergies recorded')
  })

  it('never prints an unreadable allergy list as "no allergies"', () => {
    const { container } = render(
      <DischargePrintView patient={BARE} medications={[]} allergies={null} />,
    )
    expect(container.textContent).not.toContain('No allergies recorded')
    expect(container.textContent).toMatch(/could not be read/)
  })

  it('prints a medicine as recorded, with no invented duration or route', () => {
    const { container } = render(
      <DischargePrintView
        patient={BARE}
        allergies={[]}
        medications={[{ drug_name: 'Metformin', dosage: '500 mg', frequency: 'BD', status: 'active', route: null }]}
      />,
    )
    const text = container.textContent || ''
    expect(text).toContain('Metformin')
    expect(text).not.toMatch(/FOR 5 DAYS/i)
    expect(text).not.toMatch(/\bOral\b/)
    expect(text).toMatch(/Duration: not recorded|Duration not recorded/)
  })

  it('labels a drafted summary as unreviewed and withholds nothing it was given', () => {
    const { getByTestId, container } = render(
      <DischargePrintView
        patient={BARE} medications={[]} allergies={[]}
        summary="Admitted with pneumonia." summaryStatus="draft"
      />,
    )
    expect(getByTestId('discharge-draft-label')).toHaveTextContent(/AI-drafted, not reviewed/)
    expect(container.textContent).toContain('Admitted with pneumonia.')
  })
})
