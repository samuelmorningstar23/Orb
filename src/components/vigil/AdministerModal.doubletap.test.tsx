/**
 * ONE PHYSICAL DOSE MUST PRODUCE ONE CHARTED DOSE.
 *
 * Two properties of the eMAR administration modal, one that already held and
 * one that did not:
 *
 *  1. CONTROL — a double-tap on "Given" posts once. This held before the fix
 *     (React flushes a click's state update before the next click is
 *     dispatched, so the second tap lands on a disabled button) and is asserted
 *     here so a future refactor cannot quietly remove the `disabled` guard.
 *
 *  2. THE DEFECT — a retry after a FAILED-looking attempt must not chart a
 *     second dose. A Wi-Fi drop at the bedside (the scenario this endpoint's
 *     offline support exists for) can lose the RESPONSE to a request the
 *     server already applied. The nurse sees an error, taps Given again — and
 *     because the Idempotency-Key was minted per call inside offlineMutate,
 *     the retry carried a NEW key and the server had no way to recognise it as
 *     a replay. Two doses on the MAR from one dose in the patient.
 */
import { render, screen, fireEvent, act } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const administerMedication = vi.fn()
/* `getUser` is mocked to NOBODY SIGNED IN, which is the honest reading for these
   cases: an unknown role holds no permissions (src/lib/roles.ts), so the modal
   offers no allergy-override control and these assertions are about the screen
   as a nurse sees it. The prescriber's half is
   AdministerModal.override.test.tsx, which signs a user in for real. */
vi.mock('../../api/client', () => ({
  administerMedication: (...a: unknown[]) => administerMedication(...a),
  getUser: () => null,
}))

import AdministerModal from './AdministerModal'

const MED = { id: 42, drug_name: 'Enoxaparin', dosage: '40 mg', route: 'SC', frequency: 'OD' }

function open() {
  render(
    // `[]` = a list was read and it was empty (NKDA), not "unknown". These
    // cases are about the idempotency key, not the allergy repeat.
    <AdministerModal med={MED} patientName="Anita Rao" patientBed="3" allergies={[]} onClose={() => {}} onRecorded={() => {}} />,
  )
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Enoxaparin' } })
}
const givenBtn = () => screen.getByRole('button', { name: /^Given$/ })

describe('AdministerModal — one dose, one MAR entry', () => {
  beforeEach(() => { administerMedication.mockReset() })

  it('double-tapping Given posts exactly once', async () => {
    administerMedication.mockImplementation(
      () => new Promise(res => setTimeout(() => res({ status: 'ok', administration: { id: 1 } }), 50)),
    )
    open()
    const btn = givenBtn()
    fireEvent.click(btn)
    fireEvent.click(btn)
    expect(administerMedication).toHaveBeenCalledTimes(1)
  })

  it('a retry after a lost response reuses the same Idempotency-Key', async () => {
    administerMedication.mockImplementation(() => Promise.reject(new Error('Network request failed')))
    open()

    await act(async () => { fireEvent.click(givenBtn()) })
    // The nurse sees a failure and tries again.
    expect(document.querySelector('.administer-modal__error')).toBeTruthy()
    await act(async () => { fireEvent.click(givenBtn()) })

    expect(administerMedication).toHaveBeenCalledTimes(2)
    const firstKey = administerMedication.mock.calls[0][2]
    const secondKey = administerMedication.mock.calls[1][2]

    // A key must actually be sent...
    expect(typeof firstKey).toBe('string')
    expect(firstKey).toBeTruthy()
    // ...and the retry must carry the SAME one, or the server cannot dedupe it.
    expect(secondKey).toBe(firstKey)
  })

  it('given and not-given are distinct mutations with distinct keys', async () => {
    administerMedication.mockImplementation(() => Promise.reject(new Error('offline')))
    open()
    await act(async () => { fireEvent.click(givenBtn()) })
    fireEvent.click(screen.getByRole('button', { name: /Not given/ }))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'Refused' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Record not given/ })) })

    const keys = administerMedication.mock.calls.map(c => c[2])
    expect(keys[0]).not.toBe(keys[1])
  })
})
