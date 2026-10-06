/** The ward clerk's Observations view under Beds (Orb Ward Record). */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.hoisted(() => vi.fn())
vi.mock('../../../api/client', () => ({
  apiFetch,
  getUser: () => ({ id: 'CLK1', name: 'Clerk', role: 'ward_clerk' }),
}))
vi.mock('../../../api/nurse', () => ({
  recordObservations: vi.fn(),
  previewNews2: vi.fn(),
}))

import ObservationsView, * as view from './ObservationsView'

const BOARD = {
  wards: [{
    code: 'GM1', name: 'General Medicine 1',
    tiles: [
      { bed_label: '10', unit_code: 'GM1-10', occupant_hidden: false,
        occupant: { patient_id: 'P10', name: 'Bed Ten', mrn: 'M10' } },
      { bed_label: '2', unit_code: 'GM1-2', occupant_hidden: false,
        occupant: { patient_id: 'P2', name: 'Bed Two', mrn: 'M2' } },
      { bed_label: '3', unit_code: 'GM1-3', occupant_hidden: false, occupant: null },
    ],
  }],
}
const AWAITING = {
  awaiting: [],
  unlinked: [{ patient_id: 'P99', name: 'Typed Bed', mrn: null, ward: 'General Medicine 1', bed: '14' }],
}

beforeEach(() => {
  localStorage.clear()
  apiFetch.mockReset()
  apiFetch.mockImplementation(async (path: string) => (path === '/adt/board' ? BOARD : AWAITING))
})
afterEach(cleanup)

describe('ObservationsView', () => {
  it('is a Beds view only the clerk sees', () => {
    expect(view.viewId).toBe('observations')
    expect(view.viewLabel).toBe('Observations')
    expect(view.viewPermission).toBe('vitals.transcribe')
  })

  it('lists the ward by bed and opens the clerk sheet', async () => {
    await act(async () => { render(<ObservationsView onModuleOff={vi.fn()} />) })
    const names = screen.getAllByRole('listitem').map(li => li.querySelector('strong')?.textContent)
    expect(names).toEqual(['Bed Two', 'Bed Ten', 'Typed Bed'])
    fireEvent.click(screen.getAllByRole('button', { name: 'Copy from sheet' })[0])
    expect(screen.getByRole('heading', { name: 'Copy observations from the sheet' })).toBeTruthy()
    expect(screen.getByLabelText('Time on the sheet')).toBeTruthy()
  })

  it('hands a switched-off module to the panel', async () => {
    apiFetch.mockRejectedValue(Object.assign(new Error('off'), { status: 404, detail: { error: 'module_disabled' } }))
    const onModuleOff = vi.fn()
    await act(async () => { render(<ObservationsView onModuleOff={onModuleOff} />) })
    expect(onModuleOff).toHaveBeenCalled()
  })
})
