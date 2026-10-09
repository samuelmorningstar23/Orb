/**
 * The two chart prints on an Orb Ward Record computer (Samuel's ruling, 9
 * October 2026: hide the drug lists). With showMedicines false the discharge
 * print and Print record carry no medicines section at all, not even an empty
 * one, and the allergy list still prints. With it left out both print the
 * medicines as before.
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DischargePrintView } from './DischargePrintView'
import { InpatientRecordPrintView } from './InpatientRecordPrintView'

const PATIENT = {
  name: 'Ravi Kumar', mrn: 'MRN-0021', age: 58, sex: 'M', ward: 'GM-2', bed: '21',
  diagnosis: null, admission_date: null, attending_doctor_name: null, admission_id: null,
}
const ALLERGIES = [{ substance: 'Penicillin', reaction: 'Anaphylaxis', severity: 'severe' }]
const MEDS = [{ drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral', frequency: 'TDS',
                status: 'active' as const }]
const MEDICINE_WORDS = /medicine|medication|Amoxicillin|500 mg/i

describe('the discharge print', () => {
  it('has no medicines section on a Ward Record computer, and keeps the allergy', () => {
    const { container } = render(
      <DischargePrintView patient={PATIENT} allergies={ALLERGIES} medications={[]}
                          unverifiedMedicationCount={2} showMedicines={false} />,
    )
    expect(container.textContent).not.toMatch(MEDICINE_WORDS)
    expect(container.textContent).toContain('Penicillin')
  })

  it('prints the medicines with the switch left out', () => {
    const { container } = render(
      <DischargePrintView patient={PATIENT} allergies={ALLERGIES} medications={MEDS} />,
    )
    expect(container.textContent).toContain('DISCHARGE MEDICINES')
    expect(container.textContent).toContain('Amoxicillin')
  })
})

describe('Print record', () => {
  it('has no medicines section on a Ward Record computer, and keeps the allergy', () => {
    const { container } = render(
      <InpatientRecordPrintView patient={PATIENT} allergies={ALLERGIES} problems={[]}
                                medications={[]} notes={[]} showMedicines={false} />,
    )
    expect(container.textContent).not.toMatch(MEDICINE_WORDS)
    expect(container.textContent).toContain('Penicillin')
  })

  it('prints the active medicines with the switch left out', () => {
    const { container } = render(
      <InpatientRecordPrintView patient={PATIENT} allergies={ALLERGIES} problems={[]}
                                medications={MEDS} notes={[]} />,
    )
    expect(container.textContent).toContain('ACTIVE MEDICINES')
    expect(container.textContent).toContain('Amoxicillin')
  })
})
