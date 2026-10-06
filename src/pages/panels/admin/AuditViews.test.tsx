/**
 * The audit screens read their filter lists from the trail, search on Enter
 * rather than per keystroke, show a summary in full on request, and the
 * Security tab opens on the failures and denials instead of burying them.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()
const getAuditLogs = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
  getAuditLogs: (...args: unknown[]) => getAuditLogs(...args),
}))

import AuditLogTable from './AuditLogTable'
import SecurityActivity from './SecurityActivity'

const LONG_SUMMARY =
  'Submitted to medication administration: completed, with a summary long enough to be clipped'

const LOG = {
  id: 1, timestamp: '2026-10-03 01:00:00', correlation_id: 'corr-1234567890',
  event_category: 'phi_access', action_type: 'patient_chart_viewed', outcome: 'failure',
  actor_user_id: 'ADM001', actor_role: 'admin', patient_id: 'P404', source_type: null,
  summary_message: LONG_SUMMARY, metadata_json: null, target_type: null, target_id: null,
  request_path: '/api/vigil/patients/P404/chart', ip_address: '127.0.0.1',
}

beforeEach(() => {
  apiFetch.mockReset()
  getAuditLogs.mockReset()
  apiFetch.mockImplementation((path: string) => {
    if (path.startsWith('/audit/facets')) {
      return Promise.resolve({
        categories: ['authentication', 'phi_access'],
        actions: ['login_failure', 'patient_chart_viewed'],
        outcomes: ['success', 'failure', 'partial_success', 'denied'],
      })
    }
    return Promise.reject(new Error(`unexpected ${path}`))
  })
  getAuditLogs.mockResolvedValue({ logs: [LOG], total: 1, limit: 25, offset: 0 })
})

describe('AuditLogTable', () => {
  it('offers the categories the trail holds, in words, and never the dead "auth" value', async () => {
    render(<AuditLogTable onTraceClick={() => {}} />)
    const category = await screen.findByLabelText('Category')
    await waitFor(() => expect(screen.getByRole('option', { name: 'PHI access' })).toBeTruthy())
    const values = Array.from((category as HTMLSelectElement).options).map(o => o.value)
    expect(values).toEqual(['', 'authentication', 'phi_access'])
    expect(screen.getByRole('option', { name: 'Failures and denials' })).toBeTruthy()
  })

  it('searches on Enter, not on every keystroke', async () => {
    render(<AuditLogTable onTraceClick={() => {}} />)
    await waitFor(() => expect(getAuditLogs).toHaveBeenCalledTimes(1))
    const input = screen.getByLabelText('Patient ID')
    fireEvent.change(input, { target: { value: 'P4' } })
    fireEvent.change(input, { target: { value: 'P404' } })
    expect(getAuditLogs).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(getAuditLogs).toHaveBeenCalledTimes(2))
    expect(getAuditLogs.mock.calls[1][0]).toMatchObject({ patient_id: 'P404', offset: 0 })
  })

  it('opens a clipped summary to its full text', async () => {
    render(<AuditLogTable onTraceClick={() => {}} />)
    const toggle = await screen.findByRole('button', { name: LONG_SUMMARY })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
  })
})

describe('SecurityActivity', () => {
  it('opens on failures and denials across the three security categories', async () => {
    render(<SecurityActivity />)
    await waitFor(() => expect(getAuditLogs).toHaveBeenCalled())
    expect(getAuditLogs.mock.calls[0][0]).toMatchObject({
      event_category: 'authentication,authorization,security',
      outcome: 'failure,denied',
    })
    expect((screen.getByLabelText('Outcome') as HTMLSelectElement).value).toBe('failure,denied')
  })

  it('filters by an action read from the trail', async () => {
    render(<SecurityActivity />)
    await screen.findByRole('option', { name: 'Login failure' })
    fireEvent.change(screen.getByLabelText('Action'), { target: { value: 'login_failure' } })
    await waitFor(() => expect(getAuditLogs.mock.calls.at(-1)?.[0]).toMatchObject({
      action_type: 'login_failure', offset: 0,
    }))
  })

  it('says plainly when there are no failures, and keeps the filter on screen', async () => {
    getAuditLogs.mockResolvedValue({ logs: [], total: 0, limit: 25, offset: 0 })
    render(<SecurityActivity />)
    expect(await screen.findByText(/No failed sign-ins, refusals or denied requests/)).toBeTruthy()
    expect(screen.getByLabelText('Outcome')).toBeTruthy()
  })
})

describe('the log shows names', () => {
  it('renders the actor as "Name (id)" when the server resolved a name', async () => {
    getAuditLogs.mockResolvedValue({
      logs: [{ ...LOG, actor_user_id: 'D-101', actor_name: 'Dr Kavya Menon' }],
      total: 1, limit: 25, offset: 0,
    })
    render(<AuditLogTable onTraceClick={() => {}} />)
    expect(await screen.findByText('Dr Kavya Menon (D-101)')).toBeTruthy()
  })

  it('falls back to the id alone for a user who is gone', async () => {
    getAuditLogs.mockResolvedValue({
      logs: [{ ...LOG, actor_user_id: 'GONE-1', actor_name: null }],
      total: 1, limit: 25, offset: 0,
    })
    render(<AuditLogTable onTraceClick={() => {}} />)
    expect(await screen.findByText('GONE-1')).toBeTruthy()
  })
})
