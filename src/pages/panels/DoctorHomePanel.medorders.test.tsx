/**
 * The doctor's home: medication orders to sign and the medicines a nurse
 * charted (medorders-10, W9-D).
 *
 * Pinned:
 *   * with the order store off, or for an account that cannot prescribe, the
 *     home asks nothing new and looks as it always did;
 *   * with it on, 'Pending my decision' counts the unsigned medication orders
 *     on the ward, says how many waited over a day, and opens Orders;
 *   * 'Entered by a nurse' lists the nurse-charted medicines, each opening the
 *     patient (for visibility only: ruling R010 is held);
 *   * every request is still a GET; a failed read is said, never a calm zero.
 */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DoctorHomePanel from './DoctorHomePanel'
import { resetFeatureFlags, setFeatureFlags } from '../../shellConfig'

const WARD = 'General Medicine'

const BOARD = {
  generated_at: new Date().toISOString(), department: WARD, patients: [],
  totals: { patients: 0, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
}

function queued(id: string, over: Record<string, unknown> = {}) {
  return {
    id, patient_id: 'P-04', status: 'draft', version: 1, drug_name: 'Ceftriaxone',
    entered_by: 'NUR1', entered_by_role: 'nurse', entered_by_name: 'Ravi Iyer',
    patient_name: 'Anita Rao', patient_ward: WARD, patient_bed: 'GM-04',
    created_at: '2026-10-09T08:00:00Z', ...over,
  }
}

const TO_SIGN = {
  drafts: [queued('MO-1', { waiting_over_24h: true }), queued('MO-2', { waiting_over_24h: false })],
  entered_by_a_nurse: [queued('MO-3', { status: 'prescribed', drug_name: 'Paracetamol',
    patient_id: 'P-11', patient_name: 'Vikas Shah', patient_bed: 'GM-11' })],
  draft_wait_hours: 24,
  counts: { drafts: 2, waiting_over_24h: 1, entered_by_a_nurse: 1 },
}

interface Call { url: string; method: string }
const calls: Call[] = []
let routes: Record<string, unknown | null> = {}

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async (url: unknown, init: RequestInit = {}) => {
    const u = String(url)
    calls.push({ url: u, method: String(init.method ?? 'GET').toUpperCase() })
    const hit = Object.entries(routes).find(([fragment]) => u.includes(fragment))
    if (!hit || hit[1] === null) {
      return { ok: false, status: 500, headers: { get: () => null },
        json: async () => ({ detail: `stub: no route for ${u}` }) }
    }
    return { ok: true, status: 200, headers: { get: () => null }, json: async () => hit[1] }
  }))
}

const bus: { type: string; detail: unknown }[] = []
const listener = (e: Event) => bus.push({ type: e.type, detail: (e as CustomEvent).detail })

async function home(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: 'DOC001', name: 'Dr Iyer', role,
    department: WARD }))
  render(<DoctorHomePanel />)
  await screen.findByRole('heading', { name: 'Today', level: 1 })
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
}

const medorderCalls = () => calls.filter(c => c.url.includes('/medorders/'))

beforeEach(() => {
  localStorage.clear()
  calls.length = 0
  bus.length = 0
  routes = {
    '/nurse/board': BOARD, '/relay/alerts': [], '/scribe/notes': [], '/chart/unseen': { counts: {} },
    '/outcome/shift-status': { generated_at: new Date().toISOString(), status_available: true,
      shifts: [] },
    '/medorders/queue/to-sign': TO_SIGN,
  }
  stubFetch()
  window.addEventListener('orb:goto-tab', listener)
  window.addEventListener('orb:open-patient', listener)
})

afterEach(() => {
  cleanup()
  window.removeEventListener('orb:goto-tab', listener)
  window.removeEventListener('orb:open-patient', listener)
  resetFeatureFlags()
  vi.unstubAllGlobals()
})

describe('DoctorHomePanel: medication orders (medorders-10)', () => {
  it('asks nothing new while the order store is off', async () => {
    await home('doctor')
    await screen.findByText('Nothing is waiting on you.')
    expect(medorderCalls()).toEqual([])
    expect(screen.queryByRole('heading', { name: /Entered by a nurse/ })).toBeNull()
  })

  it('asks nothing for an account that cannot prescribe', async () => {
    setFeatureFlags({ med_order_store: true })
    await home('nurse')
    expect(medorderCalls()).toEqual([])
  })

  it('counts the orders to sign, says the oldest waited, and opens Orders', async () => {
    setFeatureFlags({ med_order_store: true })
    await home('doctor')
    const pending = (await screen.findByText(/2 medication orders awaiting signature/))
      .closest('section') as HTMLElement
    expect(within(pending).getByText(/1 waiting over 24 hours/)).toBeTruthy()
    fireEvent.click(within(pending).getByRole('button', { name: /medication orders awaiting/ }))
    expect(bus).toContainEqual({ type: 'orb:goto-tab', detail: { tab: 'orders' } })
    expect(calls.every(c => c.method === 'GET')).toBe(true)
  })

  it('lists the medicines a nurse charted, each opening the patient', async () => {
    setFeatureFlags({ med_order_store: true })
    await home('doctor')
    const heading = await screen.findByRole('heading', { name: /Entered by a nurse/ })
    const list = heading.closest('section') as HTMLElement
    const row = within(list).getByRole('button', { name: /Paracetamol for Vikas Shah/ })
    expect(row.textContent).toMatch(/GM-11 · Vikas Shah: Paracetamol/)
    expect(row.textContent).toMatch(/Entered by Ravi Iyer/)
    fireEvent.click(row)
    expect(bus).toContainEqual({ type: 'orb:open-patient',
      detail: { patientId: 'P-11', name: 'Vikas Shah' } })
  })

  it('says a failed queue read instead of a calm zero', async () => {
    setFeatureFlags({ med_order_store: true })
    routes['/medorders/queue/to-sign'] = null
    await home('doctor')
    expect(await screen.findByText(/Couldn't check medication orders to sign/)).toBeTruthy()
    expect(screen.queryByText('Nothing is waiting on you.')).toBeNull()
  })
})
