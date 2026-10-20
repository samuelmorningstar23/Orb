/**
 * FrontDeskPanel and the group identity match (multisite-17).
 *
 * The card appears only while the server reports ORB_MULTI_SITE on AND more
 * than one facility on record, and only for a central desk (the server derived
 * no department for the account). On every install with one facility the desk
 * is exactly what it was.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetPlatformFlags, setPlatformFlags } from '../../shellConfig'
import FrontDeskPanel from './FrontDeskPanel'

const apiFetch = vi.fn()

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  getUser: () => ({ role: 'admin' }),
}))

function route(department: string | null) {
  apiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith('/frontdesk/opd/queue')) {
      return { visit_date: '2026-10-20', department, queues: [], total: 0 }
    }
    if (path.startsWith('/frontdesk/opd/doctors')) {
      return { doctors: [{ id: 'DOC-9', name: 'Dr Halvorsen', department: 'Satellite Medicine' }] }
    }
    if (path.startsWith('/vigil/patients')) return []
    if (path.startsWith('/frontdesk/transfers/destinations')) {
      return { destinations: [{ department: 'Satellite Medicine', staff_count: 2, wards: [] }] }
    }
    throw new Error(`unrouted apiFetch path in test: ${path}`)
  })
}

const TITLE = 'Patient seen at another facility'

beforeEach(() => {
  apiFetch.mockReset()
  resetPlatformFlags()
})

afterEach(() => {
  cleanup()
  resetPlatformFlags()
  vi.clearAllMocks()
})

describe('FrontDeskPanel group identity match', () => {
  it('is not offered with one facility on record', async () => {
    route(null)
    setPlatformFlags({ multi_site: true, multi_facility_data: false })
    render(<FrontDeskPanel holds={() => true} views={[]} />)
    expect(await screen.findByText('Register a visit')).toBeInTheDocument()
    expect(screen.queryByText(TITLE)).toBeNull()
  })

  it('is not offered with the multi-site switch off', async () => {
    route(null)
    setPlatformFlags({ multi_site: false, multi_facility_data: true })
    render(<FrontDeskPanel holds={() => true} views={[]} />)
    expect(await screen.findByText('Register a visit')).toBeInTheDocument()
    expect(screen.queryByText(TITLE)).toBeNull()
  })

  it('is offered to a central desk once more than one facility is on record', async () => {
    route(null)
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    render(<FrontDeskPanel holds={() => true} views={[]} />)
    expect(await screen.findByText(TITLE)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Search all facilities' })).toBeDisabled()
  })

  it('is not offered to an account the server scoped to a department', async () => {
    route('Satellite Medicine')
    setPlatformFlags({ multi_site: true, multi_facility_data: true })
    render(<FrontDeskPanel holds={() => true} views={[]} />)
    expect(await screen.findByText('Register a visit')).toBeInTheDocument()
    expect(screen.queryByText(TITLE)).toBeNull()
  })
})
