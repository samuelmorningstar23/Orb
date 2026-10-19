/**
 * One medication order's history (medorders-10, W9-D).
 *
 * Pinned: the ledger reads oldest first in plain words; who did each step is
 * said by role, never by a login id; the written reason and a dose given
 * before the pharmacist's check are shown; an empty ledger and a failed read
 * are two different sentences; a new version reloads the history; a 'Dose
 * given' step whose administration record was given across a charted allergy
 * carries the 'Across charted allergy' chip and the written reason.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    apiFetch: vi.fn(),
    socket: { on: vi.fn(), off: vi.fn() },
    // The real one: the allergy override marker has one derivation.
    givenAcrossAllergy: actual.givenAcrossAllergy,
  }
})

const fetchMedOrderEvents = vi.fn()

vi.mock('../../api/medorders', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/medorders')>()
  return { ...actual, fetchMedOrderEvents: (...a: unknown[]) => fetchMedOrderEvents(...a) }
})

import MedOrderTimeline from './MedOrderTimeline'
import { ledgerAdministration, medOrderEventActorWords, type MedOrderEvent } from '../../api/medorders'
import type { AdministrationRecord } from '../../api/client'

function event(id: number, kind: string, over: Partial<MedOrderEvent> = {}): MedOrderEvent {
  return {
    id, med_order_id: 'MO-1', patient_id: 'P1', kind, from_status: null, to_status: null,
    actor_id: 'DOC1', actor_role: 'doctor', entry_point: null, reason: null, ref_type: null,
    ref_id: null, payload: null, created_at: `2026-10-09T0${id}:00:00Z`, ...over,
  }
}

beforeEach(() => { fetchMedOrderEvents.mockReset() })
afterEach(() => { cleanup() })

describe('MedOrderTimeline', () => {
  it('reads the ledger oldest first in words, by role, with the reasons', async () => {
    fetchMedOrderEvents.mockResolvedValue([
      event(1, 'created'),
      event(2, 'signed'),
      event(3, 'held', { reason: 'Nil by mouth for theatre' }),
      event(4, 'administered', { actor_id: 'NUR1', actor_role: 'nurse',
        payload: { before_pharmacist_check: true } }),
      event(5, 'not_given', { actor_id: 'NUR1', actor_role: 'nurse', reason: 'Refused' }),
    ])
    render(<MedOrderTimeline orderId="MO-1" version={2} drugName="Amoxicillin" />)
    const steps = await screen.findAllByRole('listitem')
    expect(steps.map(s => s.querySelector('.medorder-timeline__what')?.textContent)).toEqual([
      'Order written', 'Signed by the prescriber', 'Put on hold', 'Dose given', 'Dose not given',
    ])
    expect(steps[0].textContent).toMatch(/by a doctor/)
    expect(steps[3].textContent).toMatch(/by a nurse/)
    expect(steps[3].textContent).toMatch(/Given before the pharmacist checked the order/)
    expect(steps[2].textContent).toMatch(/Reason: Nil by mouth for theatre/)
    expect(screen.queryByText(/DOC1|NUR1/)).toBeNull()
    expect(screen.getByRole('region', { name: 'History of Amoxicillin' })).toBeTruthy()
  })

  it('says an empty history and a failed read differently', async () => {
    fetchMedOrderEvents.mockResolvedValueOnce([])
    const { unmount } = render(<MedOrderTimeline orderId="MO-1" />)
    expect(await screen.findByText('Nothing is recorded on this order yet.')).toBeTruthy()
    unmount()
    fetchMedOrderEvents.mockRejectedValueOnce(new Error('Server unreachable.'))
    fetchMedOrderEvents.mockResolvedValueOnce([event(1, 'created')])
    render(<MedOrderTimeline orderId="MO-1" />)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(/not an\s+empty history/)).toBeTruthy()
    expect(screen.queryByText('Nothing is recorded on this order yet.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Order written')).toBeTruthy()
  })

  it('reloads when the order moves to a new version', async () => {
    fetchMedOrderEvents.mockResolvedValue([event(1, 'created')])
    const { rerender } = render(<MedOrderTimeline orderId="MO-1" version={1} />)
    await screen.findByText('Order written')
    rerender(<MedOrderTimeline orderId="MO-1" version={2} />)
    await waitFor(() => expect(fetchMedOrderEvents).toHaveBeenCalledTimes(2))
  })

  it('names roles in words and never prints an unknown role as a code', () => {
    expect(medOrderEventActorWords({ actor_role: 'pharmacist', actor_id: 'PH1' })).toBe('a pharmacist')
    expect(medOrderEventActorWords({ actor_role: null, actor_id: 'system' })).toBe('Orb')
    expect(medOrderEventActorWords({ actor_role: 'ward_clerk', actor_id: 'C1' })).toBe('a ward clerk')
    expect(medOrderEventActorWords({ actor_role: null, actor_id: 'X' })).toBe('a colleague')
  })

  it("marks a 'Dose given' step given across a charted allergy, by its record id", async () => {
    const reason = 'Allergy de-labelled by immunology after a negative oral challenge'
    const record = (id: number, over: Partial<AdministrationRecord> = {}): AdministrationRecord => ({
      id, medication_id: 11, patient_id: 'P1', drug_name: 'Amoxicillin', status: 'given',
      dose_given: '500 mg', route_given: 'Oral', not_given_reason: null, administered_by: 'NUR1',
      administered_by_name: 'Ravi Iyer', administered_at: '2026-10-09T10:00:00Z',
      allergy_check: null, dose_check: null, ...over,
    })
    const across = record(501, { allergy_check: { decision: 'block', alerts: [],
      override: { reason, carried: false } } })
    const ref = (id: string) => ({ ref_type: 'medication_administration', ref_id: id })
    fetchMedOrderEvents.mockResolvedValue([
      event(1, 'administered', ref('501')),
      event(2, 'administered', ref('502')),
      event(3, 'not_given', { ...ref('503'), reason: 'Refused' }),
      event(4, 'administered', ref('999')),
    ])
    render(<MedOrderTimeline orderId="MO-1" drugName="Amoxicillin" administrations={[
      across, record(502), record(503, { status: 'not_given', not_given_reason: 'Refused' }),
    ]} />)
    const steps = await screen.findAllByRole('listitem')
    expect(steps[0].textContent).toMatch(/Across charted allergy/)
    expect(steps[0].textContent).toContain(`Reason: ${reason}`)
    for (const step of steps.slice(1)) {
      expect(step.textContent).not.toMatch(/Across charted allergy/)
    }
    expect(ledgerAdministration([across], ref('501'))).toBe(across)
    expect(ledgerAdministration([across], { ref_type: 'helix_order', ref_id: '501' })).toBeUndefined()
    expect(ledgerAdministration([across], { ref_type: 'medication_administration', ref_id: null }))
      .toBeUndefined()
  })

  it('without the administrations it draws the history unmarked, never failing', async () => {
    fetchMedOrderEvents.mockResolvedValue([
      event(1, 'administered', { ref_type: 'medication_administration', ref_id: '501' }),
    ])
    render(<MedOrderTimeline orderId="MO-1" />)
    expect(await screen.findByText('Dose given')).toBeTruthy()
    expect(screen.queryByText('Across charted allergy')).toBeNull()
  })
})
