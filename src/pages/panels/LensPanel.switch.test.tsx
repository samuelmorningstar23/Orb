/** Lens with ORB_LENS_ENABLED off (Orb Ward Record, 6 October 2026): the tab
 *  stays, a plain notice says why before any upload, and analyse is disabled. */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  analyzeLensImage: vi.fn(),
  getPatientsList: vi.fn(async () => []),
  assignLensToPatient: vi.fn(),
  apiFetch: vi.fn(async () => { throw new Error('API 503') }),
}))
vi.mock('../../api/client', () => api)

import LensPanel from './LensPanel'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'

const NOTICE = 'Lens is switched off on this computer. It is not a certified diagnostic device.'

beforeEach(() => {
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: vi.fn(() => 'blob:lens-switch'),
    revokeObjectURL: vi.fn(),
  }))
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  vi.clearAllMocks()
})

function choose() {
  const input = screen.getByLabelText('Choose a medical image to analyse')
  fireEvent.change(input, { target: { files: [new File(['x'], 'cxr.png', { type: 'image/png' })] } })
}

describe('Lens switched off', () => {
  it('says so before any upload and disables analyse', async () => {
    act(() => { setClinicalSwitches({ lens_enabled: false }) })
    render(<LensPanel />)
    expect(screen.getByText(NOTICE)).toBeTruthy()
    choose()
    const analyse = screen.getByRole('button', { name: 'Analyze Image' }) as HTMLButtonElement
    expect(analyse.disabled).toBe(true)
    await act(async () => { fireEvent.click(analyse) })
    expect(api.analyzeLensImage).not.toHaveBeenCalled()
  })

  it('shows no notice and a working analyse control when Lens is on', () => {
    render(<LensPanel />)
    expect(screen.queryByText(NOTICE)).toBeNull()
    choose()
    expect((screen.getByRole('button', { name: 'Analyze Image' }) as HTMLButtonElement).disabled).toBe(false)
  })
})
