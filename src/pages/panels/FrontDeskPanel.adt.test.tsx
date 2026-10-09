/**
 * The front desk's Transfer button with Beds and admissions on (adt-6a).
 *
 * With the server's 'adt' module on, Transfer opens the Move screen (the bed
 * picker on bed setup, the covered departments, the handover words) for the
 * patient's active admission, and posts to /api/adt/transfers. With the
 * module off the old form is unchanged (FrontDeskPanel.test.tsx).
 */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetModuleFlags, setModuleFlags } from '../../shellConfig'
import FrontDeskPanel from './FrontDeskPanel'

const apiFetch = vi.fn()

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (detail: unknown, status: number) => {
    const message = (detail as { message?: unknown } | null)?.message
    return typeof message === 'string' ? message : `The server answered ${status}.`
  },
  getUser: () => ({ role: 'doctor' }),
}))

const PATIENTS = [
  { id: 'PAT-1', name: 'Anjali Rao', mrn: 'MRN-0001', ward: 'GM-1', bed: '4',
    admission_department: 'General Medicine', admission_id: 'ADM-1' },
]

const OPTIONS = {
  departments: ['General Medicine', 'Intensive Care'],
  master: { backtime_hours: 72, self_authoriser: null, authorisers: [], wards: [] },
}

const LOCATION = {
  admission_id: 'ADM-1', department: 'General Medicine', ward: 'GM-1', bed: '4', ward_id: null,
  bed_id: null, ward_department: null, linked: false, awaiting_bed: false, boarder: false,
}

beforeEach(() => {
  apiFetch.mockReset()
  apiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith('/frontdesk/opd/queue')) {
      return { visit_date: '2026-10-09', department: 'General Medicine', queues: [], total: 0 }
    }
    if (path.startsWith('/frontdesk/opd/doctors')) return { doctors: [] }
    if (path.startsWith('/vigil/patients')) return PATIENTS
    if (path.startsWith('/frontdesk/transfers/destinations')) return { destinations: [] }
    if (path === '/adt/move-options') return OPTIONS
    if (path === '/adt/admissions/ADM-1/location') return LOCATION
    throw new Error(`unrouted apiFetch path in test: ${path}`)
  })
})

afterEach(() => {
  cleanup()
  resetModuleFlags()
})

describe('FrontDeskPanel with beds and admissions on', () => {
  it('opens the Move screen for the active admission', async () => {
    setModuleFlags({ adt: true })
    render(<FrontDeskPanel />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Transfers' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Transfer' }))
    const dialog = await screen.findByRole('dialog')
    await act(async () => {})
    expect(within(dialog).getByRole('heading', { name: 'Move Anjali Rao' })).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Receiving department')).toBeInTheDocument()
    expect(within(dialog).queryByLabelText('Ward / bay')).toBeNull()
    expect(apiFetch).toHaveBeenCalledWith('/adt/admissions/ADM-1/location', expect.anything())
  })

  it('keeps the old form while the module is off', async () => {
    render(<FrontDeskPanel />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Transfers' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Transfer' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText('Ward / bay')).toBeInTheDocument()
    expect(apiFetch).not.toHaveBeenCalledWith('/adt/move-options', expect.anything())
  })
})
