/** "Entered in error" on a recorded observation set (Orb Ward Record). */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const voidObservation = vi.fn()
vi.mock('../../api/nurse', () => ({
  voidObservation: (...args: unknown[]) => voidObservation(...args),
}))

import VoidObservationButton from './VoidObservationButton'

function signIn(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: `U-${role}`, name: role, role }))
}

beforeEach(() => {
  localStorage.clear()
  voidObservation.mockReset()
  voidObservation.mockResolvedValue({ vitals_id: 12 })
})
afterEach(cleanup)

describe('Entered in error', () => {
  it.each(['nurse', 'doctor'])('a %s marks a set with a reason', async role => {
    signIn(role)
    const onVoided = vi.fn()
    render(<VoidObservationButton vitalsId={12} onVoided={onVoided} />)
    fireEvent.click(screen.getByRole('button', { name: /entered in error/i }))
    // No reason, no call.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Mark entered in error' })) })
    expect(voidObservation).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toBe('Say why this set is wrong.')

    fireEvent.change(screen.getByLabelText('Why is this set wrong?'), { target: { value: 'Wrong patient' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Mark entered in error' })) })
    expect(voidObservation).toHaveBeenCalledWith(12, 'Wrong patient')
    await waitFor(() => expect(onVoided).toHaveBeenCalled())
  })

  it('is not shown to the ward clerk on a set that is not her own copy', () => {
    signIn('ward_clerk')
    const { container } = render(<VoidObservationButton vitalsId={12} />)
    expect(container.textContent).toBe('')
  })

  it('is shown to the ward clerk on her own copy, and sends her reason', async () => {
    signIn('ward_clerk')
    render(<VoidObservationButton vitalsId={12} ownCopy />)
    fireEvent.click(screen.getByRole('button', { name: /entered in error/i }))
    fireEvent.change(screen.getByLabelText('Why is this set wrong?'), { target: { value: 'Pulse typed as 182, sheet says 82' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Mark entered in error' })) })
    expect(voidObservation).toHaveBeenCalledWith(12, 'Pulse typed as 182, sheet says 82')
  })

  it('is not shown on an own copy to a role that neither charts nor copies', () => {
    signIn('pharmacist')
    const { container } = render(<VoidObservationButton vitalsId={12} ownCopy />)
    expect(container.textContent).toBe('')
  })

  it('says why when the server refuses', async () => {
    signIn('nurse')
    voidObservation.mockRejectedValue(new Error('This set is already marked entered in error. Reload to see the record.'))
    render(<VoidObservationButton vitalsId={12} />)
    fireEvent.click(screen.getByRole('button', { name: /entered in error/i }))
    fireEvent.change(screen.getByLabelText('Why is this set wrong?'), { target: { value: 'typo' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Mark entered in error' })) })
    expect(screen.getByRole('alert').textContent).toBe('This set is already marked entered in error. Reload to see the record.')
  })
})
