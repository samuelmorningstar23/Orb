/**
 * The chart's allergy controls (founding ward list item 5, 21 October 2026):
 * the list reads from the structured table, an add sends reaction and
 * severity, a deactivation needs a reason before anything is sent, a failed
 * read never looks like "no allergies", and a reader without patient.write
 * gets the list and no controls.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const listAllergies = vi.fn()
const addAllergy = vi.fn()
const deactivateAllergy = vi.fn()

vi.mock('../../api/allergies', async () => {
  const real = await vi.importActual<typeof import('../../api/allergies')>('../../api/allergies')
  return {
    ...real,
    listAllergies: (...a: unknown[]) => listAllergies(...a),
    addAllergy: (...a: unknown[]) => addAllergy(...a),
    deactivateAllergy: (...a: unknown[]) => deactivateAllergy(...a),
  }
})

import AllergiesModal from './AllergiesModal'

const PENICILLIN = {
  id: 41, patient_id: 'P104', substance: 'penicillin', substance_class: 'penicillins',
  reaction: 'anaphylaxis', severity: 'severe', status: 'active', source: 'admission',
  recorded_by: 'DOC001', recorded_at: '2026-10-19T03:00:00+00:00',
}

beforeEach(() => {
  listAllergies.mockReset()
  addAllergy.mockReset()
  deactivateAllergy.mockReset()
  listAllergies.mockResolvedValue([PENICILLIN])
})

afterEach(() => cleanup())

function renderModal(canWrite = true) {
  const onChanged = vi.fn()
  const onClose = vi.fn()
  render(<AllergiesModal patientId="P104" patientName="Suresh Reddy" canWrite={canWrite} onClose={onClose} onChanged={onChanged} />)
  return { onChanged, onClose }
}

describe('the allergies modal', () => {
  it('lists the charted allergies with the reaction and the severity as charted', async () => {
    renderModal()
    const item = await screen.findByText('penicillin')
    const row = item.closest('.allergies-modal__item') as HTMLElement
    expect(row.textContent).toContain('severe')
    expect(row.textContent).toContain('Reaction: anaphylaxis')
    expect(row.textContent).toContain('admission')
  })

  it('adds an allergy with its reaction and severity and tells the chart', async () => {
    addAllergy.mockResolvedValue({ ...PENICILLIN, id: 42, substance: 'sulfa drugs', reaction: 'rash', severity: 'mild' })
    const { onChanged } = renderModal()
    await screen.findByText('penicillin')
    fireEvent.change(screen.getByLabelText('Substance'), { target: { value: ' Sulfa drugs ' } })
    fireEvent.change(screen.getByLabelText('Reaction'), { target: { value: 'rash' } })
    fireEvent.change(screen.getByLabelText('Severity'), { target: { value: 'mild' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Add allergy' })) })
    expect(addAllergy).toHaveBeenCalledWith('P104', { substance: 'Sulfa drugs', reaction: 'rash', severity: 'mild' })
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('status').textContent).toContain('sulfa drugs added to the allergy list')
    expect((screen.getByLabelText('Substance') as HTMLInputElement).value).toBe('')
  })

  it('will not add without a substance', async () => {
    renderModal()
    await screen.findByText('penicillin')
    const add = screen.getByRole('button', { name: 'Add allergy' }) as HTMLButtonElement
    expect(add.disabled).toBe(true)
    await act(async () => { fireEvent.click(add) })
    expect(addAllergy).not.toHaveBeenCalled()
  })

  it('asks for a reason before deactivating and sends it with the request', async () => {
    deactivateAllergy.mockResolvedValue({ ok: true })
    const { onChanged } = renderModal()
    await screen.findByText('penicillin')
    fireEvent.click(screen.getByRole('button', { name: 'Deactivate' }))
    const confirm = screen.getByRole('button', { name: 'Deactivate penicillin' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    await act(async () => { fireEvent.click(confirm) })
    expect(deactivateAllergy).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText(/Why is penicillin coming off the list/), {
      target: { value: 'Tolerated amoxicillin course in 2025, de-labelled after review' },
    })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Deactivate penicillin' })) })
    expect(deactivateAllergy).toHaveBeenCalledWith('P104', 41, 'Tolerated amoxicillin course in 2025, de-labelled after review')
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('status').textContent).toContain('penicillin deactivated')
  })

  it('shows the server refusal and keeps the list when a write fails', async () => {
    addAllergy.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }))
    const { onChanged } = renderModal()
    await screen.findByText('penicillin')
    fireEvent.change(screen.getByLabelText('Substance'), { target: { value: 'iodine' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Add allergy' })) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Your role cannot change the allergy list.')
    expect(onChanged).not.toHaveBeenCalled()
    expect(screen.getByText('penicillin')).toBeTruthy()
  })

  it('never shows a failed read as no allergies', async () => {
    listAllergies.mockRejectedValue(new Error('network'))
    renderModal()
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be read|network/)
    expect(screen.queryByText('No active allergies on the list.')).toBeNull()
  })

  it('offers no controls without patient.write', async () => {
    renderModal(false)
    await screen.findByText('penicillin')
    expect(screen.queryByRole('button', { name: 'Deactivate' })).toBeNull()
    expect(screen.queryByLabelText('Substance')).toBeNull()
    expect(screen.getByText(/Read only/)).toBeTruthy()
  })
})
