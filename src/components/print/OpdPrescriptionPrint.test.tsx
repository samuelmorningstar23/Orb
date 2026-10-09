/**
 * The printed outpatient prescription (opd-12): exactly what the server's
 * print answer carries, nothing invented. The allergy review is printed as
 * what it said and when, never an allergen; no date of birth or phone; the
 * doctor's registration number, or that it is not on file.
 */
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { PrescriptionPrint } from '../../api/opdPrescriptions'
import { formatClinicalDateTime } from '../../lib/datetime'
import OpdPrescriptionPrint from './OpdPrescriptionPrint'

const PRINT: PrescriptionPrint = {
  kind: 'prescription',
  printed_at: '2026-10-10T05:00:00+00:00',
  visit: { id: 'V-1', visit_date: '2026-10-10', department: 'Medicine', token_number: 7 },
  patient: { name: 'Chitra Second', uhid: 'UH-0077', age: 54, sex: 'F' },
  allergy_review: { status: 'allergies_recorded', status_label: 'Allergies recorded',
    assessed_at: '2026-10-10T04:05:00+00:00' },
  diagnoses: ['Community acquired pneumonia'],
  prescriptions: [
    { drug_name: 'Amoxicillin', dose: '500 mg', route: 'Oral', frequency: 'TDS', is_prn: false,
      prn_indication: null, duration_days: 5, quantity: 15, instructions: 'After food',
      prescribed_at: '2026-10-10T04:20:00+00:00', prescriber_name: 'Dr Iyer' },
    { drug_name: 'Paracetamol', dose: '500 mg', route: 'Oral', frequency: 'QDS', is_prn: true,
      prn_indication: 'fever above 38.5', duration_days: 1, quantity: null, instructions: null,
      prescribed_at: '2026-10-10T04:21:00+00:00', prescriber_name: 'Dr Iyer' },
  ],
  doctors: [{ name: 'Dr Iyer', registration_number: 'KMC-48213' }],
}

afterEach(() => cleanup())

describe('the printed prescription', () => {
  it('carries the patient, the review, the diagnosis and every line as given', () => {
    render(<OpdPrescriptionPrint print={PRINT} onAfterPrint={() => {}} autoPrint={false} />)
    const sheet = screen.getByTestId('opd-prescription-print')
    expect(within(sheet).getByRole('heading', { name: 'Prescription' })).toBeInTheDocument()
    expect(within(sheet).getByText('Chitra Second')).toBeInTheDocument()
    expect(within(sheet).getByText('UH-0077')).toBeInTheDocument()
    expect(within(sheet).getByText('54 y, Female')).toBeInTheDocument()
    expect(within(sheet).getByText(
      `Allergies recorded, asked ${formatClinicalDateTime('2026-10-10T04:05:00+00:00')}. `
      + 'The allergy list is in the patient\'s record.')).toBeInTheDocument()
    expect(within(sheet).getByText('Community acquired pneumonia')).toBeInTheDocument()
    const rows = within(sheet).getAllByRole('row').slice(1)
    expect(rows.map(r => Array.from(r.querySelectorAll('td')).map(td => td.textContent))).toEqual([
      ['Amoxicillin', '500 mg', 'Oral', 'TDS', '5 days', '15', 'After food'],
      ['Paracetamol', '500 mg', 'Oral', 'When needed for fever above 38.5 (QDS)', '1 day', '', ''],
    ])
    expect(within(sheet).getByText('Registration number KMC-48213')).toBeInTheDocument()
    expect(within(sheet).getByText(`Printed ${formatClinicalDateTime(PRINT.printed_at)}`)).toBeInTheDocument()
  })

  it('names no allergen, date of birth or phone, and colours nothing', () => {
    render(<OpdPrescriptionPrint print={PRINT} onAfterPrint={() => {}} autoPrint={false} />)
    const sheet = screen.getByTestId('opd-prescription-print')
    expect(sheet.textContent).not.toMatch(/penicillin|date of birth|phone/i)
    expect(sheet.querySelector('[style]')).toBeNull()
  })

  it('says when there is no review and no registration number, and leaves out an unsigned diagnosis', () => {
    render(<OpdPrescriptionPrint
      print={{ ...PRINT, allergy_review: null, diagnoses: [],
        doctors: [{ name: 'Dr Iyer', registration_number: null }] }}
      onAfterPrint={() => {}} autoPrint={false} />)
    const sheet = screen.getByTestId('opd-prescription-print')
    expect(within(sheet).getByText('No allergy review on file.')).toBeInTheDocument()
    expect(within(sheet).getByText('Registration number not on file')).toBeInTheDocument()
    expect(within(sheet).queryByText('Diagnosis')).not.toBeInTheDocument()
  })
})
