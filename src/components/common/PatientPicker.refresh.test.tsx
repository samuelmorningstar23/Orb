/**
 * A patient admitted from the shell must reach a picker that is already
 * mounted. Admitting used to reload the page; without the reload the Orders,
 * Revenue Integrity and Payer pickers kept the list they loaded on first open,
 * and a clinician who could not find the new patient might admit them again.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Patient } from '../../api/client'

const api = vi.hoisted(() => ({ getPatients: vi.fn() }))
vi.mock('../../api/client', () => api)

import { PanelActiveContext, announceDataChanged } from '../../contexts/PanelActiveContext'
import PatientPicker from './PatientPicker'

const MEERA = { id: 'PAT-1', name: 'Meera Iyer', bed: 'B-04', mrn: 'MRN-1' } as Patient
const ARJUN = { id: 'PAT-2', name: 'Arjun Nair', bed: 'B-07', mrn: 'MRN-2' } as Patient

function Picker({ active = true }: { active?: boolean }) {
  return (
    <PanelActiveContext.Provider value={active}>
      <PatientPicker value="" onChange={() => {}} />
    </PanelActiveContext.Provider>
  )
}

function openList() {
  fireEvent.focus(screen.getByRole('combobox'))
}

describe('PatientPicker after an admission', () => {
  beforeEach(() => {
    api.getPatients.mockReset()
    api.getPatients.mockResolvedValueOnce([MEERA]).mockResolvedValue([MEERA, ARJUN])
  })

  it('lists the newly admitted patient without a page reload', async () => {
    render(<Picker />)
    await screen.findByPlaceholderText('Search by name, bed or MRN')
    openList()
    expect(await screen.findByText('Meera Iyer')).toBeTruthy()
    expect(screen.queryByText('Arjun Nair')).toBeNull()

    act(() => { announceDataChanged() })

    expect(await screen.findByText('Arjun Nair')).toBeTruthy()
    expect(api.getPatients).toHaveBeenCalledTimes(2)
  })

  it('in a hidden panel, refreshes once when the panel is shown again', async () => {
    const view = render(<Picker active={false} />)
    await waitFor(() => expect(api.getPatients).toHaveBeenCalledTimes(1))

    act(() => { announceDataChanged() })
    expect(api.getPatients).toHaveBeenCalledTimes(1)

    view.rerender(<Picker active />)
    await waitFor(() => expect(api.getPatients).toHaveBeenCalledTimes(2))
    openList()
    expect(await screen.findByText('Arjun Nair')).toBeTruthy()
  })
})
