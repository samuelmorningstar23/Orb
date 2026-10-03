/**
 * DATA ERASURE IS A REQUEST THAT IS DECIDED, NOT A BUTTON THAT DELETES.
 *
 * The card used to post to /admin/patients/{id}/erase, which deleted every
 * patient table with no retention check. The server now answers that route
 * 410; this pins the screen to the adjudicated path: open a request, show each
 * category's decision with the rule that applies, and offer erasure only for
 * what was honoured and can be carried out.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))
vi.mock('../../../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

import DataPanel from './DataPanel'

const REQUEST = {
  id: 'ERQ-1', patient_id: 'P007', requested_at: '2026-10-02T09:00:00+00:00',
  status: 'recorded', note: null,
}
const DECISIONS = [
  {
    id: 'ERD-1', category: 'clinical_record', disposition: 'refused', executable: false,
    reason: 'Refused: Orb holds this on a legal_obligation basis.',
    authority: 'Indian Medical Council Regulations 2002, Reg. 1.3.1',
  },
  {
    id: 'ERD-2', category: 'ai_interaction_transcript', disposition: 'honoured', executable: true,
    reason: 'Held on a consent basis. Orb will erase it.', authority: null,
  },
]

let state: 'recorded' | 'decided' = 'recorded'

beforeEach(() => {
  state = 'recorded'
  apiFetch.mockReset()
  apiFetch.mockImplementation((path: string, opts?: RequestInit) => {
    const method = opts?.method ?? 'GET'
    if (path === '/admin/backups') return Promise.resolve({ backups: [] })
    if (path === '/admin/compliance/erasure' && method === 'GET') return Promise.resolve({ requests: [] })
    if (path === '/admin/compliance/erasure' && method === 'POST') return Promise.resolve(REQUEST)
    if (path === '/admin/compliance/erasure/ERQ-1/adjudicate') {
      state = 'decided'
      return Promise.resolve({})
    }
    if (path === '/admin/compliance/erasure/ERQ-1') {
      return Promise.resolve(state === 'recorded'
        ? { request: REQUEST, decisions: [], executions: [] }
        : { request: { ...REQUEST, status: 'decided' }, decisions: DECISIONS, executions: [] })
    }
    return Promise.reject(new Error(`unexpected ${method} ${path}`))
  })
})

describe('data erasure card', () => {
  it('opens an adjudicated request instead of calling the retired erase route', async () => {
    render(<DataPanel />)
    fireEvent.change(screen.getByLabelText('Patient ID'), { target: { value: 'P007' } })
    fireEvent.click(screen.getByRole('button', { name: 'Open erasure request' }))

    await screen.findByText(/Request ERQ-1/)
    const paths = apiFetch.mock.calls.map((c) => c[0] as string)
    expect(paths.some((p) => p.includes('/erase'))).toBe(false)
    expect(paths).toContain('/admin/compliance/erasure')
    // Recording decides nothing, so nothing can be erased yet.
    expect(screen.queryByRole('button', { name: 'Erase approved records' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Check against retention rules' }))
    await screen.findByText('Kept by law')
    expect(screen.getByText('Medical record')).toBeTruthy()
    expect(screen.getByText(/Reg\. 1\.3\.1/)).toBeTruthy()
    expect(screen.getByText('Will be erased')).toBeTruthy()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Erase approved records' })).toBeTruthy())
  })

  it('does not label the right as GDPR', async () => {
    render(<DataPanel />)
    await screen.findByText('DPDP Act 2023, right to erasure')
    expect(screen.queryByText(/GDPR/)).toBeNull()
  })
})
