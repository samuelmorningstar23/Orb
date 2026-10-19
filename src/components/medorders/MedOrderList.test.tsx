/**
 * The chart's medication orders (medorders-10, W9-D).
 *
 * Pinned:
 *   * every status says itself in words on its chip; colour is only a border
 *     (status x verify-before-dose table);
 *   * which steps are offered, for every status x permission x switch
 *     combination: nothing in record mode; sign, hold, resume and stop are the
 *     prescriber's; a draft is cancelled by whoever entered it or a
 *     prescriber; hold, resume and stop only once the server serves them;
 *   * sign posts the version on screen, says what happened, names a
 *     same-ingredient duplicate, and opens the safety window on a 428;
 *   * a step that needs a reason asks for one before anything is sent;
 *   * a stale version is said in words and the list reloads;
 *   * the supply line says 'Supply not linked' when no supply was requested;
 *   * Administer is offered only where the old table offered it;
 *   * a failed read is never drawn as an empty list;
 *   * 'medorders:update' for this patient reloads; another patient's does not;
 *   * the held refusal words match the server's sentence;
 *   * a dose given across a charted allergy carries the 'Across charted
 *     allergy' chip and its written reason, as the old table did: on the
 *     ledger's last dose (joined by ref_id), on the ward row's last given dose
 *     behind a later 'not given', on a legacy row, and in the history.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  role: 'doctor' as string,
  userId: 'DOC1' as string,
  listeners: [] as Array<(u: unknown) => void>,
}))

vi.mock('../../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    getUser: () => ({ id: h.userId, role: h.role, name: 'Signed in' }),
    isSafetyAckRequired: (err: unknown) =>
      !!err && (err as { code?: string }).code === 'safety_ack_required',
    // The real one: the allergy override marker has one derivation.
    givenAcrossAllergy: actual.givenAcrossAllergy,
  }
})

const fetchPatientMedOrders = vi.fn()
const signMedOrder = vi.fn()
const cancelMedOrderDraft = vi.fn()
const holdMedOrder = vi.fn()
const resumeMedOrder = vi.fn()
const stopMedOrder = vi.fn()
const fetchMedOrderEvents = vi.fn()

vi.mock('../../api/medorders', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/medorders')>()
  return {
    ...actual,
    fetchPatientMedOrders: (...a: unknown[]) => fetchPatientMedOrders(...a),
    signMedOrder: (...a: unknown[]) => signMedOrder(...a),
    cancelMedOrderDraft: (...a: unknown[]) => cancelMedOrderDraft(...a),
    holdMedOrder: (...a: unknown[]) => holdMedOrder(...a),
    resumeMedOrder: (...a: unknown[]) => resumeMedOrder(...a),
    stopMedOrder: (...a: unknown[]) => stopMedOrder(...a),
    fetchMedOrderEvents: (...a: unknown[]) => fetchMedOrderEvents(...a),
    subscribeMedOrderUpdates: (fn: (u: unknown) => void) => {
      h.listeners.push(fn)
      return () => { h.listeners = h.listeners.filter(l => l !== fn) }
    },
  }
})

import MedOrderList from './MedOrderList'
import {
  HELD_REFUSAL_START,
  isHeldRefusal,
  medOrderChip,
  medOrderSteps,
  supplyLineWords,
  type MedOrder,
  type MedOrderStatus,
  type MedOrderStep,
} from '../../api/medorders'
import { resetFeatureFlags, setFeatureFlags } from '../../shellConfig'
import type { AdministrationRecord, ChartMedicationRow } from '../../api/client'
import { formatClinicalDateTime } from '../../lib/datetime'

const STATUSES: MedOrderStatus[] = ['draft', 'prescribed', 'verified', 'held', 'stopped',
  'completed', 'cancelled', 'rejected']

function order(over: Partial<MedOrder> = {}): MedOrder {
  return {
    id: 'MO-1', patient_id: 'P1', admission_id: 'A1', encounter_type: 'ip', status: 'prescribed',
    held_from: null, version: 3, source: 'chart', needs_verification: false,
    drug_name: 'Amoxicillin', formulary_id: null, dose: '500 mg', route: 'Oral',
    frequency: 'TDS', is_prn: false, prn_indication: null, priority: 'routine',
    indication: null, notes: null, entered_by: 'DOC1', entered_by_role: 'doctor',
    prescriber_id: 'DOC1', prescribed_at: '2026-10-09T08:00:00Z', verified_by: null,
    verified_at: null, status_reason: null, status_changed_by: null, status_changed_at: null,
    last_safety: null, legacy_medication_id: 11, created_at: '2026-10-09T08:00:00Z',
    updated_at: '2026-10-09T08:00:00Z', fhir_status: 'active', prescriber_name: 'Dr Iyer',
    entered_by_name: 'Dr Iyer', supply: null, last_dose: null,
    ...over,
  }
}

const ROW: ChartMedicationRow = {
  id: 11, patient_id: 'P1', drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral',
  frequency: 'TDS', status: 'active', prescribed_by: 'DOC1', notes: null,
  started_at: '2026-10-09T08:00:00Z', unverified: false,
}

function answer(orders: MedOrder[], legacy: unknown[] = []) {
  fetchPatientMedOrders.mockResolvedValue({ patient_id: 'P1', orders, legacy })
}

function renderList(props: Partial<Parameters<typeof MedOrderList>[0]> = {}) {
  const onAdminister = vi.fn()
  const onChanged = vi.fn()
  render(
    <MedOrderList patientId="P1" chartRows={[ROW]} administrations={[]} canAdminister
      onAdminister={onAdminister} onChanged={onChanged} {...props} />,
  )
  return { onAdminister, onChanged }
}

const rowOf = (drug: string) => screen.getByText(drug).closest('li') as HTMLElement

beforeEach(() => {
  h.role = 'doctor'
  h.userId = 'DOC1'
  h.listeners = []
  for (const fn of [fetchPatientMedOrders, signMedOrder, cancelMedOrderDraft, holdMedOrder,
    resumeMedOrder, stopMedOrder, fetchMedOrderEvents]) fn.mockReset()
  setFeatureFlags({ med_order_store: true, med_order_lifecycle: true })
})

afterEach(() => {
  cleanup()
  resetFeatureFlags()
})

// ── Words and steps, for every combination ───────────────────────────────────

describe('the status chip says the status in words', () => {
  const expected: Record<MedOrderStatus, [string, string]> = {
    draft: ['Awaiting signature', 'warn'],
    prescribed: ['Prescribed', 'neutral'],
    verified: ['Verified by pharmacist', 'ok'],
    held: ['On hold', 'danger'],
    stopped: ['Stopped', 'neutral'],
    completed: ['Completed', 'neutral'],
    cancelled: ['Cancelled', 'neutral'],
    rejected: ['Rejected by pharmacy', 'danger'],
  }
  it.each(STATUSES)('%s', status => {
    const chip = medOrderChip({ status, needs_verification: false, verified_by: null })
    expect([chip.label, chip.tone]).toEqual(expected[status])
  })

  it('a prescribed order waiting for a pharmacist says so, by either rule', () => {
    expect(medOrderChip({ status: 'prescribed', needs_verification: true }).label)
      .toBe('Awaiting pharmacist')
    expect(medOrderChip({ status: 'prescribed', needs_verification: false, verified_by: null },
      { verifyBeforeDose: true }).label).toBe('Awaiting pharmacist')
    expect(medOrderChip({ status: 'prescribed', needs_verification: false, verified_by: 'PH1' },
      { verifyBeforeDose: true }).label).toBe('Prescribed')
  })
})

describe('which steps are offered, every status x permission x switch', () => {
  const expected = (status: MedOrderStatus, prescriber: boolean, creator: boolean,
    holdStop: boolean): MedOrderStep[] => {
    if (status === 'draft') {
      return [...(prescriber ? ['sign' as const] : []),
        ...(prescriber || creator ? ['cancel' as const] : [])]
    }
    if (!prescriber || !holdStop) return []
    if (status === 'prescribed' || status === 'verified') return ['hold', 'stop']
    if (status === 'held') return ['resume', 'stop']
    return []
  }
  for (const status of STATUSES) {
    for (const prescriber of [true, false]) {
      for (const creator of [true, false]) {
        for (const holdStop of [true, false]) {
          it(`${status}, prescriber ${prescriber}, creator ${creator}, hold routes ${holdStop}`, () => {
            const o = { status, entered_by: creator ? 'ME' : 'SOMEONE' }
            expect(medOrderSteps(o, { mayPrescribe: prescriber, userId: 'ME', lifecycleOn: true,
              holdStopAvailable: holdStop })).toEqual(expected(status, prescriber, creator, holdStop))
            expect(medOrderSteps(o, { mayPrescribe: prescriber, userId: 'ME', lifecycleOn: false,
              holdStopAvailable: holdStop })).toEqual([])
          })
        }
      }
    }
  }
})

describe('the supply line', () => {
  it("says 'Supply not linked' when no supply was ever requested", () => {
    expect(supplyLineWords({ supply: null })).toBe('Supply not linked')
  })
  it('names who dispensed and who received', () => {
    const words = supplyLineWords({ supply: {
      helix_order_id: 'HX1', state: 'received', at: '2026-10-09T10:00:00Z',
      requested_at: '2026-10-09T08:00:00Z',
      dispensed: { by: 'PH1', by_name: 'Leela Das', by_role: 'pharmacist', at: '2026-10-09T09:00:00Z' },
      received: { by: 'NUR1', by_name: 'Ravi Iyer', by_role: 'nurse', at: '2026-10-09T10:00:00Z' },
    } })
    expect(words).toMatch(/^Dispensed by Leela Das, received on the ward by Ravi Iyer at /)
  })
})

describe('the held refusal', () => {
  it("matches the bedside's own sentence on the server", () => {
    const core = readFileSync(join(process.cwd(), 'server/services/medorders/core.py'), 'utf8')
    expect(core).toContain(`_HELD_WORDS = ("${HELD_REFUSAL_START}`)
    expect(isHeldRefusal(`${HELD_REFUSAL_START}, so a dose cannot be given.`)).toBe(true)
    expect(isHeldRefusal('This medicine was stopped, so a dose cannot be given.')).toBe(false)
  })
})

// ── The list on screen ───────────────────────────────────────────────────────

describe('MedOrderList on the chart', () => {
  it('draws each order with its chip, prescriber, supply line and last dose', async () => {
    answer([
      order(),
      order({ id: 'MO-2', drug_name: 'Paracetamol', legacy_medication_id: null, status: 'verified',
        verified_by: 'PH1', verified_by_name: 'Leela Das', verified_at: '2026-10-09T09:00:00Z',
        last_dose: { kind: 'not_given', reason: 'Refused', by: 'NUR1', by_name: 'Ravi Iyer',
          by_role: 'nurse', at: '2026-10-09T12:00:00Z' } }),
    ])
    renderList()
    const amox = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    expect(within(amox).getByText('Prescribed')).toBeTruthy()
    expect(within(amox).getByText(/Prescribed by Dr Iyer/)).toBeTruthy()
    expect(within(amox).getByText('Supply not linked')).toBeTruthy()
    const para = rowOf('Paracetamol')
    expect(within(para).getByText('Verified by pharmacist')).toBeTruthy()
    expect(within(para).getByText(/Checked by Leela Das/)).toBeTruthy()
    expect(within(para).getByText(/Last dose not given \(Refused\), recorded by Ravi Iyer/)).toBeTruthy()
  })

  it('a prescriber signs a draft with the version on screen and is told what happened', async () => {
    answer([order({ status: 'draft', version: 4 })])
    signMedOrder.mockResolvedValue({ order: order(), medication_id: 11, safety: null,
      duplicates: [{ med_order_id: null, medication_id: 3, drug_name: 'Amoxil', status: 'active' }],
      replayed: false })
    const { onChanged } = renderList()
    const row = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    expect(within(row).getByText('Awaiting signature')).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Sign' }))
    await waitFor(() => expect(signMedOrder).toHaveBeenCalledWith('MO-1', 4, undefined))
    expect(await screen.findByText(/Signed\. It is on the ward chart.*Already on this patient's chart: Amoxil/))
      .toBeTruthy()
    expect(onChanged).toHaveBeenCalled()
    expect(fetchPatientMedOrders).toHaveBeenCalledTimes(2)
  })

  it('a 428 at sign opens the safety window, and the acknowledgement is sent with the signature', async () => {
    answer([order({ status: 'draft' })])
    signMedOrder.mockRejectedValueOnce(Object.assign(new Error('ack'), {
      code: 'safety_ack_required',
      safety: { allergy_alerts: [], interaction_alerts: [{ severity: 'moderate', message: 'QT' }],
        max_severity: 'moderate', requires_acknowledgement: true },
    }))
    signMedOrder.mockResolvedValueOnce({ order: order(), medication_id: 11, safety: null,
      duplicates: [], replayed: false })
    renderList()
    fireEvent.click(await screen.findByRole('button', { name: 'Sign' }))
    const dialog = await screen.findByRole('dialog')
    const reasonBox = within(dialog).getByRole('textbox')
    fireEvent.change(reasonBox, { target: { value: 'Benefit outweighs the QT risk, ECG daily' } })
    const confirm = within(dialog).getAllByRole('button').find(b => /acknowledge|proceed|continue/i
      .test(b.textContent || '')) as HTMLButtonElement
    fireEvent.click(confirm)
    await waitFor(() => expect(signMedOrder).toHaveBeenLastCalledWith('MO-1', 3,
      { acknowledged: true, reason: 'Benefit outweighs the QT risk, ECG daily' }))
  })

  it('a nurse sees no Sign on a draft, and may cancel only the draft she entered, with a reason', async () => {
    h.role = 'nurse'
    h.userId = 'NUR1'
    answer([
      order({ status: 'draft', entered_by: 'NUR1', entered_by_role: 'nurse', entered_by_name: 'Ravi Iyer' }),
      order({ id: 'MO-2', drug_name: 'Paracetamol', status: 'draft', entered_by: 'DOC9' }),
    ])
    cancelMedOrderDraft.mockResolvedValue(order({ status: 'cancelled' }))
    renderList()
    const mine = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    expect(within(mine).queryByRole('button', { name: 'Sign' })).toBeNull()
    expect(within(rowOf('Paracetamol')).queryByRole('button', { name: 'Cancel' })).toBeNull()
    fireEvent.click(within(mine).getByRole('button', { name: 'Cancel' }))
    const submit = within(mine).getByRole('button', { name: 'Cancel' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    expect(cancelMedOrderDraft).not.toHaveBeenCalled()
    fireEvent.change(within(mine).getByLabelText('Why is it being cancelled?'),
      { target: { value: 'Entered on the wrong patient' } })
    fireEvent.click(within(mine).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(cancelMedOrderDraft).toHaveBeenCalledWith('MO-1',
      'Entered on the wrong patient', 3))
  })

  it('hold, resume and stop wait for the server to serve them', async () => {
    answer([order(), order({ id: 'MO-2', drug_name: 'Heparin', status: 'held',
      status_reason: 'Nil by mouth for theatre', status_changed_by_name: 'Dr Iyer',
      status_changed_at: '2026-10-09T11:00:00Z' })])
    renderList()
    await screen.findByText('Amoxicillin')
    expect(screen.queryByRole('button', { name: 'Hold' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
    const held = rowOf('Heparin')
    expect(within(held).getByText('On hold')).toBeTruthy()
    expect(within(held).getByText(/by Dr Iyer: Nil by mouth for theatre\. Do not give/)).toBeTruthy()
  })

  it('with the routes served a prescriber holds with a reason, and a stale version reloads', async () => {
    answer([order()])
    holdMedOrder.mockRejectedValue(Object.assign(new Error('stale'), {
      status: 409, detail: { error: 'stale_version' } }))
    renderList({ holdStopAvailable: true })
    const row = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    fireEvent.click(within(row).getByRole('button', { name: 'Hold' }))
    fireEvent.change(within(row).getByLabelText('Why is it on hold?'), { target: { value: 'NBM' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Hold' }))
    await waitFor(() => expect(holdMedOrder).toHaveBeenCalledWith('MO-1', 'NBM', 3))
    expect(await screen.findByText(/This order changed since the list was loaded/)).toBeTruthy()
    await waitFor(() => expect(fetchPatientMedOrders).toHaveBeenCalledTimes(2))
  })

  it('a nurse is offered no hold, resume or stop even with the routes served', async () => {
    h.role = 'nurse'
    answer([order(), order({ id: 'MO-2', drug_name: 'Heparin', status: 'held' })])
    renderList({ holdStopAvailable: true })
    await screen.findByText('Amoxicillin')
    for (const name of ['Hold', 'Resume', 'Stop', 'Sign']) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
  })

  it('record mode (lifecycle off) offers no step at all and says the list is for reading', async () => {
    setFeatureFlags({ med_order_lifecycle: false })
    answer([order({ status: 'draft' }), order({ id: 'MO-2', drug_name: 'Heparin' })])
    renderList({ holdStopAvailable: true })
    await screen.findByText('Amoxicillin')
    for (const name of ['Sign', 'Cancel', 'Hold', 'Stop']) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.getByText(/This list is for reading on this computer/)).toBeTruthy()
  })

  it('offers Administer only for a live order on an active chart row, and never on a held one', async () => {
    answer([order(), order({ id: 'MO-2', drug_name: 'Heparin', status: 'held', legacy_medication_id: 12 })])
    const heldRow = { ...ROW, id: 12, drug_name: 'Heparin' }
    const { onAdminister } = renderList({ chartRows: [ROW, heldRow] })
    const amox = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    fireEvent.click(within(amox).getByRole('button', { name: 'Administer' }))
    expect(onAdminister).toHaveBeenCalledWith(ROW)
    expect(within(rowOf('Heparin')).queryByRole('button', { name: 'Administer' })).toBeNull()
  })

  it('offers no Administer on the Orb Ward Record', async () => {
    answer([order()])
    renderList({ canAdminister: false })
    await screen.findByText('Amoxicillin')
    expect(screen.queryByRole('button', { name: 'Administer' })).toBeNull()
  })

  it('a failed read is said, never drawn as an empty list', async () => {
    fetchPatientMedOrders.mockRejectedValue(new Error('Server unreachable.'))
    renderList()
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(/does\s+not mean the patient has no medicines/)).toBeTruthy()
    expect(screen.queryByText(/No medicines are ordered/)).toBeNull()
  })

  it('reloads on medorders:update for this patient only', async () => {
    answer([order()])
    renderList()
    await screen.findByText('Amoxicillin')
    expect(fetchPatientMedOrders).toHaveBeenCalledTimes(1)
    act(() => { for (const fn of h.listeners) fn({ patient_id: 'P2', med_order_id: 'X', status: 'held', version: 2 }) })
    expect(fetchPatientMedOrders).toHaveBeenCalledTimes(1)
    act(() => { for (const fn of h.listeners) fn({ patient_id: 'P1', med_order_id: 'MO-1', status: 'held', version: 4 }) })
    await waitFor(() => expect(fetchPatientMedOrders).toHaveBeenCalledTimes(2))
  })

  it("reloads when the chart's ward rows change, as after the chart's own Add form", async () => {
    // The chart's Add form posts to the chart's route, which pushes nothing on
    // the socket; the chart reloads the rows it hands in, and the list follows.
    answer([order()])
    const administrations: AdministrationRecord[] = []
    const fixed = { patientId: 'P1', administrations, canAdminister: true, onAdminister: vi.fn() }
    const { rerender } = render(<MedOrderList {...fixed} chartRows={[ROW]} />)
    await screen.findByText('Amoxicillin')
    expect(fetchPatientMedOrders).toHaveBeenCalledTimes(1)

    // The same rows in a new array (the chart re-rendered): nothing to read again.
    rerender(<MedOrderList {...fixed} chartRows={[{ ...ROW }]} />)
    expect(fetchPatientMedOrders).toHaveBeenCalledTimes(1)

    // A second medicine charted from the Add form.
    const added: ChartMedicationRow = { ...ROW, id: 12, drug_name: 'Paracetamol', dosage: '1 g', frequency: 'QDS' }
    answer([order(), order({ id: 'MO-2', drug_name: 'Paracetamol', dose: '1 g', frequency: 'QDS',
      legacy_medication_id: 12, version: 1 })])
    rerender(<MedOrderList {...fixed} chartRows={[ROW, added]} />)
    expect(await screen.findByText('Paracetamol')).toBeTruthy()
    expect(fetchPatientMedOrders).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('heading', { name: 'Current (2)' })).toBeTruthy()

    // A stopped one recorded from the Add form (status Discontinued).
    const stopped: ChartMedicationRow = { ...ROW, id: 13, drug_name: 'Ceftriaxone', dosage: '1 g',
      route: 'IV', frequency: 'OD', status: 'discontinued' }
    answer([order(), order({ id: 'MO-2', drug_name: 'Paracetamol', dose: '1 g', frequency: 'QDS',
      legacy_medication_id: 12, version: 1 }),
    order({ id: 'MO-3', drug_name: 'Ceftriaxone', status: 'stopped', legacy_medication_id: 13 })])
    rerender(<MedOrderList {...fixed} chartRows={[ROW, added, stopped]} />)
    expect(await screen.findByRole('button', { name: 'Show 1 ended order' })).toBeTruthy()
    expect(fetchPatientMedOrders).toHaveBeenCalledTimes(3)
  })

  it('an empty chart handed in as a new list each render reads once, not on every render', async () => {
    answer([])
    const administrations: AdministrationRecord[] = []
    const fixed = { patientId: 'P1', administrations, canAdminister: true, onAdminister: vi.fn() }
    const { rerender } = render(<MedOrderList {...fixed} chartRows={[]} />)
    await screen.findByText('No medicines are ordered for this patient.')
    rerender(<MedOrderList {...fixed} chartRows={[]} />)
    rerender(<MedOrderList {...fixed} chartRows={[]} />)
    expect(fetchPatientMedOrders).toHaveBeenCalledTimes(1)
  })

  it('an older read landing after a newer one does not put the superseded list back', async () => {
    let releaseFirst: (v: unknown) => void = () => {}
    fetchPatientMedOrders.mockImplementationOnce(() => new Promise(resolve => { releaseFirst = resolve }))
    fetchPatientMedOrders.mockResolvedValueOnce({ patient_id: 'P1', legacy: [],
      orders: [order({ status: 'held', status_reason: 'Nil by mouth for theatre', version: 4 })] })
    renderList()
    act(() => { for (const fn of h.listeners) fn({ patient_id: 'P1', med_order_id: 'MO-1', status: 'held', version: 4 }) })
    expect(await screen.findByText('On hold')).toBeTruthy()
    await act(async () => { releaseFirst({ patient_id: 'P1', legacy: [], orders: [order()] }) })
    expect(screen.getByText('On hold')).toBeTruthy()
    expect(screen.queryByText('Prescribed')).toBeNull()
  })

  it('lists chart rows the order record has not taken in, with Administer', async () => {
    answer([], [{ legacy: true, medication_id: 11, drug_name: 'Amoxicillin', dose: '500 mg',
      route: 'Oral', frequency: 'TDS', chart_status: 'active', status: 'prescribed', is_prn: false,
      needs_verification: false, verified_by: null, verified_at: null, started_at: null,
      adoptable: true }])
    const { onAdminister } = renderList()
    const row = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    expect(within(row).getByText(/Charted before the order record/)).toBeTruthy()
    fireEvent.click(within(row).getByRole('button', { name: 'Administer' }))
    expect(onAdminister).toHaveBeenCalledWith(ROW)
  })

  it('keeps ended orders folded away until asked', async () => {
    answer([order({ status: 'stopped', status_reason: 'Course complete early' })])
    renderList()
    const toggle = await screen.findByRole('button', { name: 'Show 1 ended order' })
    expect(screen.queryByText('Amoxicillin')).toBeNull()
    fireEvent.click(toggle)
    expect(within(rowOf('Amoxicillin')).getByText('Reason: Course complete early')).toBeTruthy()
  })
})

// ── A dose given across a charted allergy is never silent ───────────────────

const OVERRIDE_REASON = 'Allergy de-labelled by immunology after a negative oral challenge'

function administration(over: Partial<AdministrationRecord> = {}): AdministrationRecord {
  return {
    id: 501, medication_id: 11, patient_id: 'P1', drug_name: 'Amoxicillin', status: 'given',
    dose_given: '500 mg', route_given: 'Oral', not_given_reason: null, administered_by: 'DOC1',
    administered_by_name: 'Dr Iyer', administered_at: '2026-10-09T10:00:00Z',
    allergy_check: null, dose_check: null,
    ...over,
  }
}

function acrossAllergy(over: Partial<AdministrationRecord> = {}): AdministrationRecord {
  return administration({
    allergy_check: { decision: 'block', alerts: [],
      override: { reason: OVERRIDE_REASON, carried: false, by: 'DOC1', at: '2026-10-09T10:00:00Z' } },
    ...over,
  })
}

function givenDose(refId: string, at = '2026-10-09T10:00:00Z') {
  return { kind: 'administered' as const, reason: null, by: 'DOC1', by_name: 'Dr Iyer',
    by_role: 'doctor', at, ref_type: 'medication_administration', ref_id: refId }
}

describe('a dose given across a charted allergy', () => {
  it("marks the ledger's last dose with the chip and the written reason, joined by its record id", async () => {
    // legacy_medication_id null: only the ledger's ref_id can find the record.
    answer([
      order({ legacy_medication_id: null, last_dose: givenDose('501') }),
      order({ id: 'MO-2', drug_name: 'Paracetamol', legacy_medication_id: null,
        last_dose: givenDose('502') }),
    ])
    renderList({ administrations: [
      acrossAllergy({ id: 501, medication_id: 41 }),
      administration({ id: 502, medication_id: 42, drug_name: 'Paracetamol',
        allergy_check: { decision: 'warn', alerts: [], override: null } }),
    ] })
    const amox = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    const chip = within(amox).getByText('Across charted allergy')
    expect(chip.getAttribute('title')).toBe(`Given despite a charted allergy: ${OVERRIDE_REASON}`)
    expect(within(amox).getByText(new RegExp(`Reason:\\s+${OVERRIDE_REASON}`))).toBeTruthy()
    // A warn-grade screen that never stopped anything is not an override.
    expect(within(rowOf('Paracetamol')).queryByText('Across charted allergy')).toBeNull()
  })

  it("a later 'not given' does not hide the dose given across the allergy before it", async () => {
    answer([order({ last_dose: { kind: 'not_given', reason: 'Refused', by: 'NUR1',
      by_name: 'Ravi Iyer', by_role: 'nurse', at: '2026-10-09T12:00:00Z',
      ref_type: 'medication_administration', ref_id: '503' } })])
    renderList({ administrations: [
      administration({ id: 503, status: 'not_given', not_given_reason: 'Refused',
        administered_by: 'NUR1', administered_by_name: 'Ravi Iyer',
        administered_at: '2026-10-09T12:00:00Z' }),
      acrossAllergy({ id: 501, administered_at: '2026-10-09T10:00:00Z' }),
    ] })
    const row = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    expect(within(row).getByText(/Last dose not given \(Refused\)/)).toBeTruthy()
    expect(within(row).getByText('Across charted allergy')).toBeTruthy()
    const when = formatClinicalDateTime('2026-10-09T10:00:00Z')
    expect(within(row).getByText(new RegExp(`despite a charted allergy at ${when}\\.`))).toBeTruthy()
  })

  it('marks the chart row fallback and a row the order record has not taken in', async () => {
    answer([order({ last_dose: null })], [{ legacy: true, medication_id: 12,
      drug_name: 'Ceftriaxone', dose: '2 g', route: 'IV', frequency: 'OD', chart_status: 'active',
      status: 'prescribed', is_prn: false, needs_verification: false, verified_by: null,
      verified_at: null, started_at: null, adoptable: true }])
    renderList({
      chartRows: [ROW, { ...ROW, id: 12, drug_name: 'Ceftriaxone' }],
      administrations: [
        acrossAllergy({ id: 501, medication_id: 11 }),
        acrossAllergy({ id: 502, medication_id: 12, drug_name: 'Ceftriaxone',
          allergy_check: { decision: 'block', alerts: [],
            override: { reason: 'Desensitisation step 3 under 1:1 observation', carried: true } } }),
      ],
    })
    const amox = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    expect(within(amox).getByText(/Last dose given by Dr Iyer/)).toBeTruthy()
    expect(within(amox).getByText('Across charted allergy')).toBeTruthy()
    const cef = rowOf('Ceftriaxone')
    expect(within(cef).getByText(/Charted before the order record/)).toBeTruthy()
    expect(within(cef).getByText('Across charted allergy')).toBeTruthy()
    expect(within(cef).getByText(
      /Reason carried from an earlier dose:\s+Desensitisation step 3 under 1:1 observation/)).toBeTruthy()
  })

  it('a clean dose carries no mark', async () => {
    answer([order({ last_dose: givenDose('501') })])
    renderList({ administrations: [administration({ id: 501,
      allergy_check: { decision: 'allow', alerts: [], allergies_on_file: 1 } })] })
    await screen.findByText('Amoxicillin')
    expect(screen.queryByText('Across charted allergy')).toBeNull()
  })

  it("marks the history's 'Dose given' step whose record was given across the allergy", async () => {
    answer([order({ last_dose: givenDose('502', '2026-10-09T14:00:00Z') })])
    fetchMedOrderEvents.mockResolvedValue([
      { id: 1, med_order_id: 'MO-1', patient_id: 'P1', kind: 'administered', from_status: null,
        to_status: null, actor_id: 'DOC1', actor_role: 'doctor', entry_point: null, reason: null,
        ref_type: 'medication_administration', ref_id: '501', payload: null,
        created_at: '2026-10-09T10:00:00Z' },
      { id: 2, med_order_id: 'MO-1', patient_id: 'P1', kind: 'administered', from_status: null,
        to_status: null, actor_id: 'NUR1', actor_role: 'nurse', entry_point: null, reason: null,
        ref_type: 'medication_administration', ref_id: '502', payload: null,
        created_at: '2026-10-09T14:00:00Z' },
    ])
    renderList({ administrations: [
      administration({ id: 502, administered_at: '2026-10-09T14:00:00Z' }),
      acrossAllergy({ id: 501 }),
    ] })
    const row = rowOf(await screen.findByText('Amoxicillin').then(() => 'Amoxicillin'))
    // The row's last dose (502) is clean, but the ward row's last GIVEN record
    // is 502 too, so the row itself carries no mark...
    expect(within(row).queryByText('Across charted allergy')).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'History' }))
    const history = await screen.findByRole('region', { name: 'History of Amoxicillin' })
    const steps = await within(history).findAllByRole('listitem')
    // ...and the history marks the earlier dose that was.
    expect(within(steps[0]).getByText('Across charted allergy')).toBeTruthy()
    expect(within(steps[0]).getByText(new RegExp(OVERRIDE_REASON))).toBeTruthy()
    expect(within(steps[1]).queryByText('Across charted allergy')).toBeNull()
  })
})
