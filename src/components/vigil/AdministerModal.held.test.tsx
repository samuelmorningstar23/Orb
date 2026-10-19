/**
 * A HELD ORDER AT THE BEDSIDE (medorders-10, W9-D).
 *
 * The eMAR refuses a given dose of a medication order on hold with a flat 422
 * sentence (server/services/medorders/core.py _HELD_WORDS). The modal used to
 * print it as a generic error and leave Given armed. Now it says the medicine
 * is on hold, turns Given off, and offers the one record that is always
 * allowed, not given (held), with the reason filled in. Any other refusal is
 * shown as before.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const administerMedication = vi.fn()
vi.mock('../../api/client', () => ({
  administerMedication: (...a: unknown[]) => administerMedication(...a),
  getUser: () => ({ id: 'NUR1', role: 'nurse' }),
}))

import AdministerModal from './AdministerModal'

const MED = { id: 11, drug_name: 'Enoxaparin', dosage: '40 mg', route: 'SC', frequency: 'OD' }
const HELD = 'This medicine is on hold, so a dose cannot be given. You can still record it as not given (held).'

function open(onRecorded = vi.fn()) {
  render(
    <AdministerModal med={MED} patientName="Asha Menon" patientBed="GM-4" allergies={[]}
      onClose={vi.fn()} onRecorded={onRecorded} />,
  )
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Enoxaparin' } })
  return { onRecorded }
}

describe('AdministerModal: a medication order on hold', () => {
  beforeEach(() => { administerMedication.mockReset() })

  it('says the medicine is on hold, turns Given off and records not given (held) in one press', async () => {
    administerMedication.mockRejectedValueOnce(Object.assign(new Error(HELD), { status: 422 }))
    const record = { id: 5, medication_id: 11, status: 'not_given', patient_id: 'P1' }
    administerMedication.mockResolvedValueOnce({ status: 'success', administration: record })
    const { onRecorded } = open()

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Given$/ })) })
    expect(screen.getByRole('heading', { name: 'This medicine is on hold' })).toBeTruthy()
    expect(screen.getByText(HELD)).toBeTruthy()
    const given = screen.getByRole('button', { name: /^Given$/ }) as HTMLButtonElement
    expect(given.disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Record as not given (held)' }))
    expect((screen.getByLabelText('Reason not given') as HTMLSelectElement).value)
      .toBe('Held (clinical decision)')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record not given' })) })
    const [, payload] = administerMedication.mock.calls[1]
    expect(payload).toMatchObject({ status: 'not_given', not_given_reason: 'Held (clinical decision)',
      notes: 'The order is on hold' })
    expect(onRecorded).toHaveBeenCalledWith(record)
  })

  it('leaves every other refusal as it was', async () => {
    administerMedication.mockRejectedValueOnce(
      Object.assign(new Error('This medicine was stopped, so a dose cannot be given.'), { status: 422 }))
    open()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Given$/ })) })
    expect(screen.queryByRole('heading', { name: 'This medicine is on hold' })).toBeNull()
    expect(screen.getByText('This medicine was stopped, so a dose cannot be given.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Record as not given (held)' })).toBeNull()
  })
})
