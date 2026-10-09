/**
 * The medication order behind a Helix supply row, in the review modal
 * (medorders-5c).
 *
 * A row LINKED to a medication order shows the order's state in words,
 * 'Order verified by X at T' when a pharmacist has checked it, and one history
 * of the supply's steps and the order's own life. An unlinked row (every row
 * on a site that never used the order store) asks the server for nothing new.
 * A refusal about the medication order never overwrites the supply row on
 * screen: the order is read again instead.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getHelixOrderSafety = vi.fn()
const transitionHelixOrder = vi.fn()
const getHelixOrderTimeline = vi.fn()
const getHelixDispenseOptions = vi.fn()
const dispenseHelixOrder = vi.fn()

vi.mock('../../api/client', () => ({
  getHelixOrderSafety: (...args: unknown[]) => getHelixOrderSafety(...args),
  transitionHelixOrder: (...args: unknown[]) => transitionHelixOrder(...args),
  isSafetyAckRequired: (e: unknown) => !!e && (e as { code?: string }).code === 'safety_ack_required',
}))

vi.mock('../../api/pharmacy', () => ({
  getHelixOrderTimeline: (...args: unknown[]) => getHelixOrderTimeline(...args),
  getHelixDispenseOptions: (...args: unknown[]) => getHelixDispenseOptions(...args),
  dispenseHelixOrder: (...args: unknown[]) => dispenseHelixOrder(...args),
}))

import HelixOrderReviewModal from './HelixOrderReviewModal'
import { resetFeatureFlags, resetModuleFlags, setFeatureFlags } from '../../shellConfig'
import { formatClinicalDateTime } from '../../lib/datetime'
import type { HelixLinkedOrder } from '../../api/medorders'

const base = {
  id: 'SUP-1',
  patient_name: 'Asha Rao',
  medication_name: 'Quorvex 250mg Tablet',
  dosage: 'REVIEW REQUIRED',
  frequency: 'REVIEW REQUIRED',
  route: 'Review',
  prescribed_by_name: 'Dr Mehta',
  created_at: '2026-10-09 08:00:00',
}

const clean = {
  allergy_alerts: [], interaction_alerts: [], max_severity: 'none', blocking: false,
  interaction_screened: true, coverage_note: null,
}

const VERIFIED_AT = '2026-10-09T09:15:00+00:00'

const verifiedOrder = {
  id: 'MO-1', status: 'verified' as const, version: 3, verified_by: 'PH1',
  verified_at: VERIFIED_AT, verified_by_name: 'Ravi Kumar', needs_verification: true,
}

const waitingOrder = {
  id: 'MO-1', status: 'prescribed' as const, version: 1, verified_by: null,
  verified_at: null, verified_by_name: null, needs_verification: true,
}

function timeline(linked: HelixLinkedOrder = verifiedOrder) {
  return {
    order_id: 'SUP-1',
    patient_id: 'P1',
    linked_order: linked,
    transitions: [
      { id: 'T1', from_status: 'pending', to_status: 'reviewing', actor_id: 'PH1',
        actor_name: 'Ravi Kumar', created_at: '2026-10-09T08:30:00+00:00' },
    ],
    order_events: [
      { id: 1, kind: 'created', from_status: null, to_status: 'prescribed', actor_id: 'D1',
        actor_role: 'doctor', actor_name: 'Dr Mehta', reason: null, ref_type: null,
        ref_id: null, created_at: '2026-10-09T08:00:00+00:00' },
      { id: 2, kind: 'supply_requested', from_status: 'prescribed', to_status: 'prescribed',
        actor_id: 'D1', actor_role: 'doctor', actor_name: 'Dr Mehta', reason: null,
        ref_type: 'helix_order', ref_id: 'SUP-1', created_at: '2026-10-09T08:00:01+00:00' },
      // This supply's own step, also on the order's ledger: listed once.
      { id: 3, kind: 'supply_reviewing', from_status: 'prescribed', to_status: 'prescribed',
        actor_id: 'PH1', actor_role: 'pharmacist', actor_name: 'Ravi Kumar', reason: null,
        ref_type: 'helix_order', ref_id: 'SUP-1', created_at: '2026-10-09T08:30:00+00:00' },
      // An earlier supply of the same order, rejected: listed with its reason.
      { id: 4, kind: 'supply_rejected', from_status: 'prescribed', to_status: 'prescribed',
        actor_id: 'PH2', actor_role: 'pharmacist', actor_name: 'Meena Iyer',
        reason: 'Out of stock this week', ref_type: 'helix_order', ref_id: 'SUP-0',
        created_at: '2026-10-09T08:10:00+00:00' },
      { id: 5, kind: 'verified', from_status: 'prescribed', to_status: 'verified',
        actor_id: 'PH1', actor_role: 'pharmacist', actor_name: 'Ravi Kumar', reason: null,
        ref_type: null, ref_id: null, created_at: VERIFIED_AT },
    ],
  }
}

describe('HelixOrderReviewModal and the linked medication order', () => {
  beforeEach(() => {
    getHelixOrderSafety.mockReset()
    transitionHelixOrder.mockReset()
    getHelixOrderTimeline.mockReset()
    getHelixDispenseOptions.mockReset()
    dispenseHelixOrder.mockReset()
    getHelixOrderSafety.mockResolvedValue({
      order_id: 'SUP-1', medication_id: 'M', drug_name: base.medication_name,
      is_substitute: false, safety: clean,
    })
  })

  afterEach(() => {
    // Inside act: the flags notify the still-mounted modal.
    act(() => {
      resetFeatureFlags()
      resetModuleFlags()
    })
  })

  it('an unlinked row asks for no history and shows no order section', async () => {
    render(<HelixOrderReviewModal order={{ ...base, status: 'reviewing' }} catalog={[]} onClose={() => {}} onTransition={() => {}} />)
    expect(await screen.findByRole('button', { name: 'Verify' })).toBeInTheDocument()
    expect(getHelixOrderTimeline).not.toHaveBeenCalled()
    expect(screen.queryByText('Medication order')).not.toBeInTheDocument()
    expect(screen.queryByText('Order history')).not.toBeInTheDocument()
  })

  it('says who verified the order and when, in words', async () => {
    getHelixOrderTimeline.mockResolvedValue(timeline())
    render(<HelixOrderReviewModal
      order={{ ...base, status: 'reviewing', med_order_id: 'MO-1', linked_order: verifiedOrder }}
      catalog={[]} onClose={() => {}} onTransition={() => {}} />)

    expect(screen.getByText('Medication order')).toBeInTheDocument()
    // The state is said in words, never by colour alone.
    expect(screen.getByText('Checked by a pharmacist')).toBeInTheDocument()
    const when = formatClinicalDateTime(VERIFIED_AT)
    expect(screen.getByText(
      `Order verified by Ravi Kumar at ${when}. Verifying here checks this supply only.`,
    )).toBeInTheDocument()
    expect(getHelixOrderTimeline).toHaveBeenCalledWith('SUP-1')
    await screen.findByRole('list', { name: 'Order history' })
  })

  it('lists one history, oldest first, with each step once and reasons kept', async () => {
    getHelixOrderTimeline.mockResolvedValue(timeline())
    render(<HelixOrderReviewModal
      order={{ ...base, status: 'reviewing', med_order_id: 'MO-1', linked_order: verifiedOrder }}
      catalog={[]} onClose={() => {}} onTransition={() => {}} />)

    const list = await screen.findByRole('list', { name: 'Order history' })
    const items = within(list).getAllByRole('listitem').map(li => li.textContent ?? '')
    expect(items.map(t => t.split(formatClinicalDateTime('2026-10-09T08:00:00+00:00'))[0])).toHaveLength(5)
    expect(items[0]).toContain('Order written, by Dr Mehta')
    expect(items[1]).toContain('Pharmacy asked to supply it, by Dr Mehta')
    expect(items[2]).toContain('Supply rejected: Out of stock this week, by Meena Iyer')
    expect(items[3]).toContain('Pharmacy began the review, by Ravi Kumar')
    expect(items[4]).toContain('Order checked by a pharmacist, by Ravi Kumar')
    // The supply's own review step is not listed twice.
    expect(items.filter(t => t.includes('review'))).toHaveLength(1)
    expect(items.join(' ')).not.toMatch(/supply_|_/)
  })

  it('tells the pharmacist whether a Helix decision is the order check', async () => {
    getHelixOrderTimeline.mockResolvedValue(timeline(waitingOrder))
    const { unmount } = render(<HelixOrderReviewModal
      order={{ ...base, status: 'reviewing', med_order_id: 'MO-1', linked_order: waitingOrder }}
      catalog={[]} onClose={() => {}} onTransition={() => {}} />)
    expect(screen.getByText('Prescribed')).toBeInTheDocument()
    expect(screen.getByText(/in the Verify Queue\. Verifying here checks this supply only\./)).toBeInTheDocument()
    await screen.findByRole('list', { name: 'Order history' })
    unmount()

    act(() => { setFeatureFlags({ med_order_store: true, med_order_lifecycle: true }) })
    render(<HelixOrderReviewModal
      order={{ ...base, status: 'reviewing', med_order_id: 'MO-1', linked_order: waitingOrder }}
      catalog={[]} onClose={() => {}} onTransition={() => {}} />)
    expect(screen.getByText(/Verifying or rejecting this supply is that check\./)).toBeInTheDocument()
    await screen.findByRole('list', { name: 'Order history' })
  })

  it('says so when the history does not load', async () => {
    getHelixOrderTimeline.mockRejectedValue(new Error('down'))
    render(<HelixOrderReviewModal
      order={{ ...base, status: 'reviewing', med_order_id: 'MO-1', linked_order: waitingOrder }}
      catalog={[]} onClose={() => {}} onTransition={() => {}} />)
    expect(await screen.findByText('The order history did not load.')).toBeInTheDocument()
  })

  it('a refusal about the medication order re-reads it and never overwrites the supply', async () => {
    getHelixOrderTimeline.mockResolvedValue(timeline(waitingOrder))
    const stale = Object.assign(new Error("This medicine's order changed while you were working on it. Reload it and try again."), {
      status: 409,
      detail: {
        error: 'stale_version',
        message: "This medicine's order changed while you were working on it. Reload it and try again.",
        order: { id: 'MO-1', status: 'verified', version: 4 },
      },
    })
    transitionHelixOrder.mockRejectedValue(stale)
    const onClose = vi.fn()
    const onTransition = vi.fn()
    render(<HelixOrderReviewModal
      order={{ ...base, status: 'reviewing', med_order_id: 'MO-1', linked_order: waitingOrder }}
      catalog={[]} onClose={onClose} onTransition={onTransition} />)
    await waitFor(() => expect(getHelixOrderTimeline).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByText(/order changed while you were working on it/)).toBeInTheDocument()
    // The supply row is still the one on screen, with its own steps.
    expect(screen.getByText('Reviewing')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Verify' })).toBeInTheDocument()
    await waitFor(() => expect(getHelixOrderTimeline).toHaveBeenCalledTimes(2))
    await act(async () => { await Promise.resolve() })

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onTransition).toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })
})
