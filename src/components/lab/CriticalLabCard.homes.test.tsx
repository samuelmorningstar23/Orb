/**
 * The critical lab card on the two homes (lis-16): the nurse home shows it
 * above the view tabs, the doctor home as the first block of the reading
 * column. With the laboratory module and the interfaced switch off neither
 * home asks for it.
 *
 * Every other read the homes make is left to fail here on purpose: the card
 * must stand on its own, whatever else the home could not load.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { WardCriticalList } from '../../api/lab/wardCriticals'
import DoctorHomePanel from '../../pages/panels/DoctorHomePanel'
import NursePanel from '../../pages/panels/nurse/NursePanel'
import { resetFeatureFlags, resetModuleFlags, setModuleFlags } from '../../shellConfig'

const CARD: WardCriticalList = {
  open: [{
    id: 'LC-1', status: 'open', source_lane: 'lis', sender: 'Laboratory',
    patient: { id: 'P-1', name: 'Asha Menon', mrn: 'MRN-0042', bed: '4A', discharged: false,
      discharged_at: null },
    test_name: 'Potassium', value_display: '7.0 mmol/L', value_told: '7.0 mmol/L',
    withdrawn: false, lab_flag: { code: 'HH', text: 'Critical high' },
    critical_limit_text: 'Critical below 2.5 or above 6.5 mmol/L', reference_range: null,
    preliminary: false, state_text: 'Authorised', change_text: null,
    raised_at: '2026-10-09T04:00:00Z', acknowledged: null,
  }],
  methods: { in_app: 'Read on the ward screen', phone: 'Phone call from the laboratory' },
  recipient_roles: { nurse: 'Nurse' },
  scope: { department: 'General Medicine', hospital_wide: false },
}

const urls: string[] = []

beforeEach(() => {
  urls.length = 0
  vi.stubGlobal('fetch', vi.fn(async (url: unknown) => {
    const u = String(url)
    urls.push(u)
    if (u.includes('/lab/criticals/ward')) {
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => CARD }
    }
    return { ok: false, status: 500, headers: { get: () => null },
      json: async () => ({ detail: 'stub: not served here' }) }
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  resetModuleFlags()
  resetFeatureFlags()
})

function signIn(role: 'nurse' | 'doctor') {
  localStorage.setItem('orb_user', JSON.stringify({
    id: role === 'nurse' ? 'NUR001' : 'DOC001', name: 'Test User', role,
    department: 'General Medicine' }))
}

describe('the nurse home', () => {
  it('shows the card above the view tabs while the laboratory is on', async () => {
    signIn('nurse')
    setModuleFlags({ lab: true })
    render(<NursePanel />)
    const heading = await screen.findByRole('heading', { name: 'Critical lab results' })
    const tabs = screen.getByRole('tablist', { name: 'Nurse views' })
    expect(heading.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('Potassium 7.0 mmol/L')).toBeInTheDocument()
  })

  it('never asks for it with the laboratory and the interfaced switch off', async () => {
    signIn('nurse')
    setModuleFlags({ lab: false })
    render(<NursePanel />)
    await screen.findByRole('heading', { name: 'My shift', level: 1 })
    expect(urls.some(u => u.includes('/lab/'))).toBe(false)
    expect(screen.queryByRole('heading', { name: 'Critical lab results' })).toBeNull()
  })
})

describe('the doctor home', () => {
  it('shows the card first in the reading column while the laboratory is on', async () => {
    signIn('doctor')
    setModuleFlags({ lab: true })
    const { container } = render(<DoctorHomePanel />)
    const heading = await screen.findByRole('heading', { name: 'Critical lab results' })
    const main = container.querySelector('.today-main') as HTMLElement
    expect(main.firstElementChild?.contains(heading)).toBe(true)
  })

  it('never asks for it with the laboratory and the interfaced switch off', async () => {
    signIn('doctor')
    setModuleFlags({ lab: false })
    render(<DoctorHomePanel />)
    await screen.findByRole('heading', { name: 'Today', level: 1 })
    expect(urls.some(u => u.includes('/lab/'))).toBe(false)
  })
})
