/**
 * A DOSE THE GUARD LET THROUGH WITH A WARNING IS NOT A SILENT SUCCESS.
 *
 * The dose guard WARNS (it does not refuse) on a repeat inside the order's
 * window, and on a dose it could not check. The server charts the dose and
 * answers 200 with that verdict on `administration.dose_check`. The modal used
 * to close on the 200 without reading it, so the warning written for a stat dose
 * charted twice reached nobody.
 */
import { render, screen, fireEvent, act } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const administerMedication = vi.fn()
vi.mock('../../api/client', () => ({
  administerMedication: (...a: unknown[]) => administerMedication(...a),
  getUser: () => null,
}))

import AdministerModal from './AdministerModal'
import { doseWarnings } from './administerSafety'

const MED = { id: 11, drug_name: 'Ceftriaxone', dosage: '1 g', route: 'IV', frequency: 'STAT' }

const DUPLICATE_MSG =
  'Ceftriaxone was already given to this patient 20 minutes ago. Confirm this is not a double dose before charting.'

const RECORD = {
  id: 9, medication_id: 11, patient_id: 'P001', drug_name: 'Ceftriaxone', status: 'given',
  dose_given: '1 g', route_given: 'IV', not_given_reason: null, administered_by: 'NUR001',
  administered_by_name: 'Nurse A', administered_at: '2026-10-02 04:00:00',
  dose_check: {
    decision: 'warn', unavailable: false, unavailable_reason: null,
    findings: [{ kind: 'duplicate', decision: 'warn', message: DUPLICATE_MSG }],
  },
}

function open(onRecorded = vi.fn(), onClose = vi.fn()) {
  render(
    <AdministerModal med={MED} patientName="Asha Menon" patientBed="GM-4" allergies={[]}
      onClose={onClose} onRecorded={onRecorded} />,
  )
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Ceftriaxone' } })
  return { onRecorded, onClose }
}

describe('AdministerModal: dose-guard warnings on a 200', () => {
  beforeEach(() => { administerMedication.mockReset() })

  it('shows the WARN finding and waits for the nurse before closing', async () => {
    administerMedication.mockResolvedValue({ status: 'success', administration: RECORD })
    const { onRecorded, onClose } = open()

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Given$/ })) })

    expect(screen.getByText(DUPLICATE_MSG)).toBeTruthy()
    expect(screen.getByText(/Ceftriaxone is now on the chart/)).toBeTruthy()
    expect(onRecorded).not.toHaveBeenCalled()
    // Nothing left to press that could chart it again.
    expect(screen.queryByRole('button', { name: /^Given$/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /I have read this/ }))
    expect(onRecorded).toHaveBeenCalledWith(RECORD)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('reports the charted dose however the modal is dismissed', async () => {
    administerMedication.mockResolvedValue({ status: 'success', administration: RECORD })
    const { onRecorded, onClose } = open()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Given$/ })) })

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onRecorded).toHaveBeenCalledWith(RECORD)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes straight away on a clean 200', async () => {
    const clean = { ...RECORD, dose_check: { decision: 'allow', unavailable: false, findings: [] } }
    administerMedication.mockResolvedValue({ status: 'success', administration: clean })
    const { onRecorded } = open()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Given$/ })) })
    expect(onRecorded).toHaveBeenCalledWith(clean)
  })
})

describe('doseWarnings', () => {
  it('says out loud when the dose could not be checked', () => {
    const out = doseWarnings({
      ...RECORD,
      dose_check: {
        decision: 'warn', unavailable: true, findings: [],
        unavailable_reason: 'could not run: duplicate-dose check. This dose has NOT been fully checked',
      },
    } as never)
    expect(out).toEqual([
      'The dose check could not run: duplicate-dose check. This dose has NOT been fully checked.',
    ])
  })

  it('is empty for a dose recorded before the guard existed, and for not given', () => {
    expect(doseWarnings({ ...RECORD, dose_check: null } as never)).toEqual([])
    expect(doseWarnings({ ...RECORD, status: 'not_given' } as never)).toEqual([])
  })
})
