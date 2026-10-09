/**
 * The laboratory's Critical results view, for a critical the ward closed on
 * its own card (lis-16). The laboratory must be able to tell a ward read-back
 * from its own phone call: the closed line says it was acknowledged on the
 * ward, by whom and how, and still says a laboratory callback the way it
 * always did.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CriticalList, CriticalNotification } from '../../../api/lab/criticals'
import CriticalView from './CriticalView'

vi.mock('../../../api/lab/criticals', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/criticals')>(
    '../../../api/lab/criticals')
  return { ...actual, getCriticals: vi.fn(), logCriticalCallback: vi.fn(),
    logCriticalAttempt: vi.fn() }
})

const api = vi.mocked(await import('../../../api/lab/criticals'))

function closed(acknowledged: Record<string, unknown>): CriticalNotification {
  return {
    id: 'LC-9',
    status: 'acknowledged',
    source_lane: 'lis',
    patient: { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', ward: 'GM-1', bed: '4A',
      discharged: false, discharged_at: null },
    test_name: 'Potassium',
    value_display: '7.0 mmol/L',
    critical_limit_text: 'Critical below 2.5 or above 6.5 mmol/L',
    preliminary: true,
    result_id: 41,
    value_told: '7.0 mmol/L',
    result_now: { status: 'tech_validated', text: 'Preliminary, not yet authorised' },
    raised_at: '2026-10-09T04:00:00Z',
    raised_by_name: 'Tech A',
    raised_in_department: 'General Medicine',
    tier: { current: 1, count: 0, label: null, next_label: null, ladder_configured: false,
      due_at: null, due_now: false },
    delivery: { status: 'posted', reason: null, text: "Posted in the patient's case room.",
      message_id: 9 },
    callbacks: [],
    acknowledged: acknowledged as CriticalNotification['acknowledged'],
    events: [],
  }
}

function list(recent: CriticalNotification[]): CriticalList {
  return {
    open: [], recent,
    ladder: { configured: false, tiers: [], source: null },
    recipient_roles: { nurse: 'Nurse' }, methods: { phone: 'Phone', in_person: 'In person' },
    recent_hours: 24, can_log: true, scope: { department: null, hospital_wide: true },
  }
}

const props = { meta: {}, state: 'on' as const, openView: vi.fn(), onModuleOff: vi.fn() }

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.clearAllMocks() })

describe('a critical closed on the ward', () => {
  it('says it was acknowledged on the ward, by whom and how', async () => {
    api.getCriticals.mockResolvedValue(list([closed({
      at: '2026-10-09T04:10:00Z', by_name: 'Nurse NUR001', recipient_name: 'Sister Mary Rao',
      recipient_role: 'nurse', recipient_role_text: 'Nurse', method: 'in_app',
      method_text: 'Read on the ward screen', read_back: true, closure: 'ward_read_back' })]))
    render(<CriticalView {...props} />)
    const line = await screen.findByText(/Acknowledged on the ward by Sister Mary Rao/)
    expect(line).toHaveTextContent('(Nurse), read back (read on the ward screen)')
    expect(line).toHaveTextContent('recorded by Nurse NUR001')
    expect(line.textContent).not.toMatch(/to Sister Mary Rao/)
  })

  it("keeps the laboratory's own callback line as it was", async () => {
    api.getCriticals.mockResolvedValue(list([closed({
      at: '2026-10-09T04:10:00Z', by_name: 'Tech A', recipient_name: 'Sister Mary Rao',
      recipient_role: 'nurse', recipient_role_text: 'Nurse', method: 'phone',
      method_text: 'Phone', read_back: true, closure: 'laboratory_callback' })]))
    render(<CriticalView {...props} />)
    const line = await screen.findByText(/Phone to Sister Mary Rao/)
    expect(line).toHaveTextContent('logged by Tech A')
  })
})
