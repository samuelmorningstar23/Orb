/**
 * multisite-11: with more than one facility on record the audit screens show
 * which facility each row belongs to and let the reader narrow to one of
 * their facilities. With one facility nothing on screen changes.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()
const getAuditLogs = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
  getAuditLogs: (...args: unknown[]) => getAuditLogs(...args),
}))

import AuditLogTable from './AuditLogTable'
import { facilityLabel, fetchAuditPage } from './auditFacility'
import SecurityActivity from './SecurityActivity'
import { resetPlatformFlags, setPlatformFlags } from '../../../shellConfig'

const MAIN = { id: 1, code: 'MAIN', name: 'Vostrel General' }
const SAT = { id: 9611, code: 'QS1', name: 'Quandrevel Satellite' }

const ROW = {
  id: 7, timestamp: '2026-10-20 08:00:00', correlation_id: 'corr-facility-1',
  event_category: 'authentication', action_type: 'login_failure', outcome: 'failure',
  actor_user_id: 'D-7', actor_role: 'doctor', actor_name: 'Dr Teodric Halvanne',
  patient_id: null, source_type: null, summary_message: 'Sign-in refused', metadata_json: null,
  target_type: null, target_id: null, request_path: '/api/auth/login', ip_address: '10.0.0.4',
}

function facets(path: string) {
  if (path.startsWith('/audit/facets')) {
    return Promise.resolve({ categories: ['authentication'], actions: ['login_failure'],
      outcomes: ['success', 'failure', 'partial_success', 'denied'] })
  }
  return undefined
}

beforeEach(() => {
  apiFetch.mockReset()
  getAuditLogs.mockReset()
  resetPlatformFlags()
})

afterEach(() => resetPlatformFlags())

describe('facilityLabel', () => {
  it('names the facility with its code, and says so plainly when a row names none', () => {
    expect(facilityLabel(SAT)).toBe('Quandrevel Satellite (QS1)')
    expect(facilityLabel({ id: 3, code: 'NORTH', name: null })).toBe('NORTH')
    expect(facilityLabel({ id: 3, code: null, name: null })).toBe('Facility 3')
    expect(facilityLabel(null)).toBe('No facility recorded')
  })
})

describe('fetchAuditPage', () => {
  it('is exactly getAuditLogs without a chosen facility', async () => {
    getAuditLogs.mockResolvedValue({ logs: [], total: 0, limit: 25, offset: 0 })
    await fetchAuditPage({ limit: 25, offset: 0, outcome: 'failure' })
    expect(getAuditLogs).toHaveBeenCalledWith({ limit: 25, offset: 0, outcome: 'failure' })
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('adds the facility to the same query when one is chosen', async () => {
    apiFetch.mockResolvedValue({ logs: [], total: 0, limit: 25, offset: 0 })
    await fetchAuditPage({ limit: 25, offset: 50, event_category: 'a,b' }, 9611)
    const url = apiFetch.mock.calls[0][0] as string
    expect(url.startsWith('/audit/logs?')).toBe(true)
    const q = new URLSearchParams(url.split('?')[1])
    expect(Object.fromEntries(q)).toEqual({
      event_category: 'a,b', limit: '25', offset: '50', facility: '9611',
    })
  })
})

describe('the audit log', () => {
  it('shows no facility column or filter with one facility on record', async () => {
    apiFetch.mockImplementation((path: string) => facets(path) ?? Promise.reject(new Error(path)))
    getAuditLogs.mockResolvedValue({ logs: [ROW], total: 1, limit: 25, offset: 0 })
    render(<AuditLogTable onTraceClick={() => {}} />)
    await screen.findByText('Sign-in refused')
    expect(screen.queryByLabelText('Facility')).toBeNull()
    expect(screen.queryByRole('columnheader', { name: 'Facility' })).toBeNull()
  })

  it('labels each row and narrows to a chosen facility', async () => {
    setPlatformFlags({ multi_facility_data: true })
    getAuditLogs.mockResolvedValue({
      logs: [{ ...ROW, facility: SAT }, { ...ROW, id: 8, summary_message: 'Unplaced', facility: null }],
      total: 2, limit: 25, offset: 0, facilities: [MAIN, SAT],
    })
    apiFetch.mockImplementation((path: string) => facets(path)
      ?? Promise.resolve({ logs: [{ ...ROW, facility: SAT }], total: 1, limit: 25, offset: 0,
        facilities: [MAIN, SAT] }))
    render(<AuditLogTable onTraceClick={() => {}} />)
    await screen.findByRole('columnheader', { name: 'Facility' })
    const rows = screen.getAllByRole('row').slice(1)
    expect(within(rows[0]).getByText('Quandrevel Satellite (QS1)')).toBeTruthy()
    expect(within(rows[1]).getByText('No facility recorded')).toBeTruthy()
    const select = screen.getByLabelText('Facility') as HTMLSelectElement
    expect(Array.from(select.options).map(o => o.text)).toEqual([
      'All facilities', 'Vostrel General (MAIN)', 'Quandrevel Satellite (QS1)'])
    fireEvent.change(select, { target: { value: '9611' } })
    await waitFor(() => expect(apiFetch.mock.calls.some(
      ([p]) => typeof p === 'string' && p.startsWith('/audit/logs?') && p.includes('facility=9611'),
    )).toBe(true))
  })
})

describe('the security tab', () => {
  it('carries the facility filter into its own query', async () => {
    setPlatformFlags({ multi_facility_data: true })
    getAuditLogs.mockResolvedValue({ logs: [{ ...ROW, facility: MAIN }], total: 1, limit: 25,
      offset: 0, facilities: [MAIN, SAT] })
    apiFetch.mockImplementation((path: string) => facets(path)
      ?? Promise.resolve({ logs: [], total: 0, limit: 25, offset: 0, facilities: [MAIN, SAT] }))
    render(<SecurityActivity />)
    expect(await screen.findByText('Vostrel General (MAIN)', { selector: 'td' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Facility'), { target: { value: '1' } })
    await waitFor(() => {
      const call = apiFetch.mock.calls.find(([p]) => typeof p === 'string'
        && p.startsWith('/audit/logs?'))
      expect(call).toBeTruthy()
      const q = new URLSearchParams((call![0] as string).split('?')[1])
      expect(q.get('facility')).toBe('1')
      expect(q.get('event_category')).toBe('authentication,authorization,security')
      expect(q.get('outcome')).toBe('failure,denied')
    })
  })
})
