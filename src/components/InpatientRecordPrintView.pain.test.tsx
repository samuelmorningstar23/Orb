/**
 * Print record carries the pain score with the latest set (21 October 2026,
 * founding ward item 1): "Pain 7/10" beside the vitals, "--" when the box
 * was blank or the row predates the column.
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { InpatientRecordPrintView } from './InpatientRecordPrintView'

const PATIENT = {
  name: 'Ravi Kumar', mrn: 'MRN-0021', age: 58, sex: 'M', ward: 'GM-2', bed: '21',
  diagnosis: null, admission_date: null, attending_doctor_name: null, admission_id: null,
}
const VITALS = {
  timestamp: '2026-10-21T04:30:00+00:00', heart_rate: 84, systolic_bp: 126, diastolic_bp: 78,
  spo2: 96, temperature: 37.2, respiratory_rate: 18,
}

describe('Print record and the pain score', () => {
  it('prints the score with the latest set', () => {
    const { container } = render(
      <InpatientRecordPrintView patient={PATIENT} allergies={[]} problems={[]} medications={[]}
                                notes={[]} latestVitals={{ ...VITALS, pain_score: 7 }} />,
    )
    expect(container.textContent).toContain('RR 18/min, Pain 7/10.')
  })

  it('prints a dash for a blank box and for a set without the column', () => {
    const blank = render(
      <InpatientRecordPrintView patient={PATIENT} allergies={[]} problems={[]} medications={[]}
                                notes={[]} latestVitals={{ ...VITALS, pain_score: null }} />,
    )
    expect(blank.container.textContent).toContain('Pain --/10')
    const older = render(
      <InpatientRecordPrintView patient={PATIENT} allergies={[]} problems={[]} medications={[]}
                                notes={[]} latestVitals={VITALS} />,
    )
    expect(older.container.textContent).toContain('Pain --/10')
  })
})
