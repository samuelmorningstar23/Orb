/**
 * Revenue Integrity shows the sentence each code was matched in, and lists a
 * condition only a medicine implies under its own heading as a question for
 * the clinician, never as a code.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const RESULT = {
  patient_id: 'P001',
  suggested_codes: [{
    code: 'J44.1', system: 'ICD10', display: 'COPD with acute exacerbation',
    evidence: 'Matched “COPD exacerbation” in clinical note', confidence: 0.7,
    evidence_sentence: 'Admitted with a COPD exacerbation, wheezy and on 2 L oxygen.',
    evidence_note_id: 41, evidence_note_type: 'clinical_document', evidence_author: 'Dr Leela Varma',
  }],
  documentation_queries: [{
    condition: 'Type 2 diabetes mellitus', medication: 'Metformin 500mg', code: 'E11.9',
    system: 'ICD10', display: 'Type 2 diabetes mellitus without complications',
    message: 'Treated and not documented: ask the clinician',
  }],
  cdi_flags: [],
  reimbursement: {
    captured_inr: 0, potential_inr: 60000, uplift_inr: 60000,
    assumptions: { model: 'ILLUSTRATIVE', method: 'm', base_rate_inr: 50000, caveats: [], citations: [] },
  },
}

vi.mock('../../api/client', () => ({ apiFetch: vi.fn(async () => RESULT) }))
vi.mock('../common/PatientPicker', () => ({ default: () => null }))

import RevenueIntegrityPanel from './RevenueIntegrityPanel'

describe('RevenueIntegrityPanel', () => {
  it('shows the matched sentence and who wrote the note beside the code', async () => {
    render(<RevenueIntegrityPanel patientId="P001" />)
    expect(await screen.findByText('“Admitted with a COPD exacerbation, wheezy and on 2 L oxygen.”')).toBeTruthy()
    expect(screen.getByText('Note by Dr Leela Varma')).toBeTruthy()
  })

  it('lists a medicine-implied condition under its own heading, not as a code', async () => {
    render(<RevenueIntegrityPanel patientId="P001" />)
    expect(await screen.findByText('Documentation queries')).toBeTruthy()
    expect(screen.getByText('Treated and not documented: ask the clinician')).toBeTruthy()
    expect(screen.getByText('Treated with Metformin 500mg')).toBeTruthy()
    // Not among the suggested code chips.
    expect(screen.queryByText('E11.9')).toBeNull()
    expect(screen.getByText('Would support E11.9')).toBeTruthy()
  })
})
