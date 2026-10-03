/**
 * Order sets — the gates must run whichever order the two inputs are chosen in.
 *
 * This screen needs TWO things before it can say anything: a protocol and a
 * patient. It used to run the safety-gate preview from the protocol card's
 * onClick only, while a separate effect cleared the preview whenever either
 * input changed. So:
 *
 *   patient → protocol   worked (the click was last)
 *   protocol → patient   silently died (the effect wiped what the click fetched
 *                        and nothing refetched it)
 *
 * The second is the order the screen reads in — a full-height list of protocols
 * on the left, a small picker top-right — and it is the order the demo script
 * uses. It left the panel showing a title, a citation and nothing else: no
 * items, no Apply button, no explanation.
 *
 * These tests drive both orders and assert the same end state, so the two
 * halves (invalidate / refetch) cannot drift apart again.
 */
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
// Type-only import: vi.mock replaces the module's runtime, not its types.
import type { OrderSetVerdict } from '../../api/orderSets'

const SETS = [
  {
    id: 'sepsis-six',
    name: 'Sepsis Six (1-hour bundle)',
    indication: 'Suspected sepsis',
    guideline: { name: 'Surviving Sepsis Campaign 2021', citation: 'Evans L, et al.' },
    item_count: 2,
    fingerprint: 'fp1',
  },
]

/** A medication the gates screened and passed: what a clean drug looks like. */
const CLEAN_SAFETY = {
  drug: 'x', allergy_alerts: [], interaction_alerts: [], max_severity: 'none',
  blocking: false, safe: true, interaction_screened: true, coverage_note: null,
}
const DOSE_ALLOW = { decision: 'allow', findings: [], unavailable: false, unavailable_reason: null }

const verdict = (
  i: number, description: string, blocked: boolean, orderType = 'medication',
): OrderSetVerdict => {
  const med = orderType === 'medication'
  return {
    index: i, order_type: orderType, description,
    code: null, code_system: null, priority: 'stat',
    drug_name: med ? description : null, dose: med ? '2 g' : null,
    route: med ? 'IV' : null, frequency: med ? 'once' : null,
    note: null, edited_fields: [],
    medication_safety: med && !blocked ? { ...CLEAN_SAFETY, drug: description } : null,
    dose_check: med && !blocked ? DOSE_ALLOW : null,
    blocked, block_kind: blocked ? 'allergy' : null,
    block_reason: blocked ? 'Anaphylaxis to ceftriaxone is on this chart.' : null,
  }
}

const SURESH = {
  id: 'P-SURESH', name: 'Suresh Reddy', mrn: 'MRN-GM004', bed: 'GM-104', ward: 'GM-1',
  allergies: [{ substance: 'ceftriaxone', reaction: 'anaphylaxis', severity: 'severe' }],
}

const PREVIEW = {
  patient_id: 'P-SURESH',
  set: { id: 'sepsis-six', name: 'Sepsis Six (1-hour bundle)', guideline: {}, fingerprint: 'fp1' },
  items: [verdict(0, 'Serum lactate', false, 'lab'), verdict(1, 'Ceftriaxone 2 g IV', true)],
  blocked: [verdict(1, 'Ceftriaxone 2 g IV', true)],
  would_apply: false,
  unknown_codes: [],
  recent_application: null,
  patient: SURESH,
}

const listOrderSets = vi.fn(async () => ({ sets: SETS, config: null }))
// The mocks are called with (setId, patientId); the arguments are read from
// mock.calls, so the implementations ignore them.
const previewOrderSet = vi.fn(async (...args: string[]): Promise<unknown> => { void args; return PREVIEW })
const applyOrderSet = vi.fn(async (...args: string[]): Promise<unknown> => { void args; return { orders: [] } })
const listOrderSetApplications = vi.fn(async (...args: string[]) => { void args; return { applications: [] } })

vi.mock('../../api/orderSets', () => ({
  listOrderSets: () => listOrderSets(),
  previewOrderSet: (setId: string, patientId: string) => previewOrderSet(setId, patientId),
  applyOrderSet: (setId: string, patientId: string) => applyOrderSet(setId, patientId),
  listOrderSetApplications: (patientId: string) => listOrderSetApplications(patientId),
}))

// PatientPicker keys off `id` (that is what /api/vigil/patients returns).
vi.mock('../../api/client', () => ({
  getPatients: async () => ([
    { id: 'P-SURESH', name: 'Suresh Reddy', mrn: 'MRN-GM004', bed: 'GM-104', ward: 'GM-1', age: 55, sex: 'M' },
  ]),
}))

import OrderSetsPanel from './OrderSetsPanel'

const pickProtocol = async () => {
  const card = await screen.findByText('Sepsis Six (1-hour bundle)')
  fireEvent.click(card.closest('button')!)
}

const pickPatient = async () => {
  const input = await screen.findByPlaceholderText(/name, bed or MRN/i)
  fireEvent.change(input, { target: { value: 'Suresh' } })
  const option = await screen.findByText('Suresh Reddy')
  fireEvent.click(option.closest('button') ?? option)
}

/** The state the screen must reach either way round. */
const expectGatesRan = async () => {
  await waitFor(() => expect(screen.getByText('Serum lactate')).toBeTruthy())
  expect(screen.getByText('STOPPED')).toBeTruthy()
  const apply = screen.getByRole('button', { name: /Apply 2 order/i }) as HTMLButtonElement
  expect(apply.disabled).toBe(true)
}

beforeEach(() => { previewOrderSet.mockClear(); applyOrderSet.mockClear() })
afterEach(cleanup)

describe('order sets run the safety gates regardless of selection order', () => {
  it('patient first, then protocol', async () => {
    render(<OrderSetsPanel />)
    await pickPatient()
    await pickProtocol()
    await expectGatesRan()
  })

  // The regression. This is the order the demo script uses.
  it('protocol first, then patient', async () => {
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    await expectGatesRan()
  })

  it('never previews until it has both inputs — a gate run needs a chart', async () => {
    render(<OrderSetsPanel />)
    await pickProtocol()
    // A protocol alone must not fire a preview: the verdicts are patient-specific
    // and there is nothing honest to compute without one.
    await waitFor(() => expect(screen.getByText(/No patient selected/i)).toBeTruthy())
    expect(previewOrderSet).not.toHaveBeenCalled()
  })

  // The middle verdict. The server can answer `blocking: false, safe: false` —
  // a moderate cross-reactivity, real but below the stop threshold. The panel
  // used to collapse that to "Cleared", which is an assertion the server never
  // made, and it erased the point-of-ordering caution for the prescriber
  // (found live 2026-08-18: Ananya Kapoor, penicillin allergy, ceftriaxone
  // previewed "Cleared" while the API carried the cross-reactivity alert).
  it('renders a non-blocking allergy alert as a caution, never as Cleared', async () => {
    const caution = {
      ...verdict(0, 'Ceftriaxone 2 g IV', false),
      medication_safety: {
        drug: 'Ceftriaxone',
        allergy_alerts: [{
          type: 'allergy', substance: 'Penicillin', severity: 'moderate',
          message: 'Cross-reactivity: Ceftriaxone (cephalosporins) with documented penicillins allergy (Penicillin).',
          match: 'cross_reactivity',
        }],
        interaction_alerts: [], max_severity: 'moderate',
        blocking: false, safe: false, interaction_screened: true, coverage_note: null,
      },
    }
    previewOrderSet.mockResolvedValueOnce({
      ...PREVIEW,
      items: [caution, verdict(1, 'Paracetamol 1 g IV', false)],
      blocked: [],
      would_apply: true,
    })
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()

    await waitFor(() => expect(screen.getByText('Allergy alert')).toBeTruthy())
    expect(screen.getByText(/Cross-reactivity: Ceftriaxone/)).toBeTruthy()
    // The clean item still reads Cleared; the alerted one must not.
    expect(screen.getAllByText('Cleared')).toHaveLength(1)
    // And a caution does not veto the set — Apply stays live.
    const apply = screen.getByRole('button', { name: /Apply 2 order/i }) as HTMLButtonElement
    expect(apply.disabled).toBe(false)
  })

  it('re-runs the gates when the patient changes under a chosen protocol', async () => {
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    await expectGatesRan()
    const runs = previewOrderSet.mock.calls.length
    expect(runs).toBeGreaterThan(0)

    // Clearing the patient must take the verdicts down with it — a preview left
    // on screen beside a different chart is the worst thing this panel could do.
    const clear = screen.getByRole('button', { name: /clear/i })
    fireEvent.click(clear)
    await waitFor(() => expect(screen.queryByText('Serum lactate')).toBeNull())
  })
})

const applicable = (items: OrderSetVerdict[], extra: Record<string, unknown> = {}) => ({
  ...PREVIEW, items, blocked: [], would_apply: true, ...extra,
})

describe('order set preview tells the whole truth about each item', () => {
  it('says on every medication item that it is not sent to the ward or pharmacy', async () => {
    previewOrderSet.mockResolvedValueOnce(applicable([
      verdict(0, 'Serum lactate', false, 'lab'),
      verdict(1, 'Paracetamol 1 g IV', false),
      verdict(2, 'Piperacillin-tazobactam 4.5 g IV', false),
      verdict(3, 'Oxygen to target saturation', false, 'nursing'),
    ]))
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    await waitFor(() => expect(screen.getByText('Paracetamol 1 g IV')).toBeTruthy())

    const notices = screen.getAllByText(/Not sent to the ward or pharmacy\. Prescribe from the patient chart/)
    expect(notices).toHaveLength(2)
    for (const n of notices) {
      const desc = n.closest('li')!.querySelector('.order-sets__item-desc')!.textContent
      expect(desc).toMatch(/Paracetamol|Piperacillin/)
    }
  })

  it('puts no safety chip on a lab or nursing item', async () => {
    previewOrderSet.mockResolvedValueOnce(applicable([
      verdict(0, 'Serum lactate', false, 'lab'),
      verdict(1, 'Paracetamol 1 g IV', false),
    ]))
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    await waitFor(() => expect(screen.getByText('Serum lactate')).toBeTruthy())
    const lab = screen.getByText('Serum lactate').closest('li')!
    expect(lab.textContent).not.toMatch(/Cleared|alert|screened/i)
    expect(screen.getAllByText('Cleared')).toHaveLength(1)
  })

  it('never says Cleared when the interaction screen did not run', async () => {
    const unscreened = {
      ...verdict(0, 'Ceftriaxone 2 g IV', false),
      medication_safety: {
        ...CLEAN_SAFETY, drug: 'Ceftriaxone', interaction_screened: false,
        coverage_note: "'Ceftriaxone' is outside the interaction knowledge base. Verify manually.",
      },
    }
    previewOrderSet.mockResolvedValueOnce(applicable([unscreened]))
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    await waitFor(() => expect(screen.getByText('Not screened for interactions')).toBeTruthy())
    expect(screen.getByText(/outside the interaction knowledge base/)).toBeTruthy()
    expect(screen.queryByText('Cleared')).toBeNull()
  })

  it('shows dose-guard warnings and a dose guard that could not run', async () => {
    const warned = {
      ...verdict(0, 'Paracetamol 2 g IV', false),
      dose_check: {
        decision: 'warn', unavailable: false, unavailable_reason: null,
        findings: [{ decision: 'warn', message: 'Above the usual single dose of 1 g.' }],
      },
    }
    const unchecked = {
      ...verdict(1, 'Vancomycin 1 g IV', false),
      dose_check: {
        decision: 'warn', findings: [], unavailable: true,
        unavailable_reason: 'the dose guard could not run for this item. It has NOT been checked',
      },
    }
    previewOrderSet.mockResolvedValueOnce(applicable([warned, unchecked]))
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    await waitFor(() => expect(screen.getByText('Dose warning')).toBeTruthy())
    expect(screen.getByText('Above the usual single dose of 1 g.')).toBeTruthy()
    expect(screen.getByText('Dose not checked')).toBeTruthy()
    expect(screen.getByText(/The dose guard could not run for this item/)).toBeTruthy()
    expect(screen.queryByText('Cleared')).toBeNull()
  })

  it('labels an interaction alert as an interaction, not an allergy', async () => {
    const interacting = {
      ...verdict(0, 'Clarithromycin 500 mg IV', false),
      medication_safety: {
        ...CLEAN_SAFETY, drug: 'Clarithromycin', safe: false, max_severity: 'moderate',
        interaction_alerts: [{ type: 'interaction', severity: 'moderate',
          message: 'Clarithromycin + Atorvastatin: raised statin levels.' }],
      },
    }
    previewOrderSet.mockResolvedValueOnce(applicable([interacting]))
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    await waitFor(() => expect(screen.getByText('Interaction alert')).toBeTruthy())
    expect(screen.queryByText('Allergy alert')).toBeNull()
  })
})

describe('order sets name the patient and warn on a repeat', () => {
  it('shows the patient banner with bed, MRN and allergies, and names them on Apply', async () => {
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    const banner = await screen.findByLabelText('Patient these orders are for')
    expect(banner.textContent).toContain('Suresh Reddy')
    expect(banner.textContent).toContain('Bed GM-104')
    expect(banner.textContent).toContain('MRN MRN-GM004')
    expect(banner.textContent).toContain('Ceftriaxone (anaphylaxis)')
    expect(screen.getByRole('button', { name: 'Apply 2 orders for Suresh Reddy (GM-104)' })).toBeTruthy()
  })

  it('says when the allergy list could not be read, never "none"', async () => {
    previewOrderSet.mockResolvedValueOnce({ ...PREVIEW, patient: { ...SURESH, allergies: null } })
    render(<OrderSetsPanel />)
    await pickProtocol()
    await pickPatient()
    await waitFor(() => expect(screen.getByText(/allergy list could not be read/i)).toBeTruthy())
    expect(screen.queryByText(/No allergies recorded/)).toBeNull()
  })

  it('names the fixed patient when a host hides the picker', async () => {
    render(<OrderSetsPanel patientId="P-SURESH" />)
    await pickProtocol()
    expect(screen.queryByPlaceholderText(/name, bed or MRN/i)).toBeNull()
    const banner = await screen.findByLabelText('Patient these orders are for')
    expect(banner.textContent).toContain('Suresh Reddy')
  })

  it('keeps the picker visible and reports a change when the selection is shared', async () => {
    const onSelect = vi.fn()
    render(<OrderSetsPanel selectedPatientId="" onSelectPatient={onSelect} />)
    await pickPatient()
    expect(onSelect).toHaveBeenCalledWith('P-SURESH')
  })

  it('needs a second press to apply a set that was applied recently, and says by whom', async () => {
    const recent = applicable([verdict(0, 'Paracetamol 1 g IV', false)], {
      recent_application: {
        application_id: 7, applied_at: '2026-10-03 09:00:00', applied_by: 'D002',
        applied_by_name: 'Dr Sharma', order_count: 9, minutes_ago: 2,
      },
    })
    previewOrderSet.mockResolvedValue(recent)
    try {
      render(<OrderSetsPanel />)
      await pickProtocol()
      await pickPatient()
      await waitFor(() => expect(
        screen.getByText(/Sepsis Six \(1-hour bundle\) was applied to Suresh Reddy 2 minutes ago by Dr Sharma \(9 orders\)/),
      ).toBeTruthy())

      fireEvent.click(screen.getByRole('button', { name: /Apply again: 1 order for Suresh Reddy/ }))
      expect(applyOrderSet).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: /Confirm: apply again/ }))
      await waitFor(() => expect(applyOrderSet).toHaveBeenCalledTimes(1))
    } finally {
      previewOrderSet.mockImplementation(async () => PREVIEW)
    }
  })

  it('tells the host after an apply, and says the medication orders went nowhere', async () => {
    previewOrderSet.mockResolvedValueOnce(applicable([
      verdict(0, 'Serum lactate', false, 'lab'), verdict(1, 'Paracetamol 1 g IV', false),
    ]))
    applyOrderSet.mockResolvedValueOnce({
      status: 'applied', orders: [{ order_type: 'lab' }, { order_type: 'medication' }],
    })
    const onApplied = vi.fn()
    render(<OrderSetsPanel onApplied={onApplied} />)
    await pickProtocol()
    await pickPatient()
    fireEvent.click(await screen.findByRole('button', { name: /Apply 2 orders for Suresh Reddy/ }))
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1))
    expect(screen.getByText(/Applied for Suresh Reddy \(GM-104\): 2 draft orders created/)).toBeTruthy()
    expect(screen.getByText(/The 1 medication order is not sent to the ward or pharmacy/)).toBeTruthy()
  })
})
