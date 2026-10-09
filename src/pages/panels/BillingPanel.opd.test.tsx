/**
 * Billing, outpatients at the counter (opd-14).
 *
 *  1. FEATURE OFF, NOTHING NEW: the Charges picker never asks for the counter
 *     list and offers no outpatient group.
 *  2. A WALK-IN IS SELECTABLE: with the 'opd' feature on, an outpatient with
 *     pending outpatient lines is listed under its own group, and a patient
 *     already on the ward keeps the ward entry.
 *  3. BILL THIS VISIT sends an outpatient bill (encounter 'op', no admission)
 *     over the outpatient lines only: never a line recorded for a stay or as
 *     inpatient. No discharge advisory is asked for a walk-in.
 *  4. THE COUNTER FORM posts through the existing counter route
 *     (createOpdInvoice) with the typed lines in integer thousandths.
 *  5. THE CASHIER still has no Billing tab.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getAccessibleTabs, resetFeatureFlags, setFeatureFlags } from '../../shellConfig'

const calls: { path: string; method: string; body: unknown }[] = []

const SUMMARY = {
  patient_id: 'P9', admission_id: null, live_invoice_count: 0, draft_invoice_count: 0,
  outstanding_paise: 0, outstanding_display: '₹0.00',
  unbilled_charge_count: 3, unbilled_total_paise: 150000, unbilled_total_display: '₹1,500.00',
  unpriced_charge_count: 0,
  deposit_balance_paise: 0, deposit_balance_display: '₹0.00', open_claim_count: 0,
  net_due_paise: 150000, net_due_display: '₹1,500.00',
}

function charge(id: number, extra: Record<string, unknown>) {
  return {
    id, patient_id: 'P9', admission_id: null, encounter_type: 'op', service_code: 'CONS-OP',
    description: `Line ${id}`, quantity: 1, unit_rate_paise: 50000, rate_is_configured: 1,
    total_paise: 50000, status: 'pending', source: 'manual', service_date: '2026-10-10',
    ...extra,
  }
}

const CHARGES = {
  patient_id: 'P9',
  charges: [
    charge(21, { description: 'Outpatient consultation' }),
    // Recorded as inpatient with no admission: never on an outpatient bill.
    charge(22, { encounter_type: 'ip', description: 'Stray inpatient line' }),
    // An earlier stay's line.
    charge(23, { admission_id: 'ADM-OLD', encounter_type: 'ip', description: 'Old stay bed day' }),
    charge(24, { description: 'Dressing' }),
  ],
  summary: SUMMARY,
}

const OP_PATIENTS = {
  date: '2026-10-10',
  patient_count: 2,
  truncated: false,
  patients: [
    {
      id: 'P9', name: 'Ravi Walkin', mrn: 'UH-9', inpatient_now: false,
      visits: [{ department: 'Cardiology', doctor_name: 'Dr Iyer', token_number: 7, status: 'completed' }],
      charge_ids: [21, 24], pending_charge_count: 2, unpriced_charge_count: 0,
      priced_total_paise: 100000, priced_total_display: '₹1,000.00',
    },
    // Also on the ward: the ward entry wins.
    {
      id: 'P1', name: 'Asha Rao', mrn: 'MRN-1', inpatient_now: true, visits: [],
      charge_ids: [5], pending_charge_count: 1, unpriced_charge_count: 0,
      priced_total_paise: 50000, priced_total_display: '₹500.00',
    },
  ],
}

const INVOICE = {
  id: 31, invoice_number: 'INV-2026-27-000031', number_display: 'INV-2026-27-000031',
  patient_id: 'P9', admission_id: null, encounter_type: 'op',
  invoice_type: 'final', status: 'issued', is_interstate: 0, is_credit_note: false,
  subtotal_paise: 100000, discount_paise: 0, taxable_value_paise: 100000,
  cgst_paise: 0, sgst_paise: 0, igst_paise: 0, tax_paise: 0, total_paise: 100000,
  rounding_adjustment_paise: 0, rounding_adjustment_display: '₹0.00',
  payable_paise: 100000, payable_display: '₹1,000.00',
  amount_paid_paise: 0, amount_paid_display: '₹0.00',
  balance_paise: 100000, balance_display: '₹1,000.00',
  supersedes_invoice_id: null, issued_at: '2026-10-10 05:30:00',
  cancelled_at: null, cancel_reason: null, notes: null, lines: [],
  patient: { id: 'P9', name: 'Ravi Walkin', mrn: 'UH-9', age: 30, sex: 'M' },
  payer: null, payments: [], credit_notes: [], allowed_transitions: [],
}

const ITEMS = [{
  id: 1, code: 'CONS-OP', name: 'Outpatient consultation', category: 'consultation',
  department: null, uom: 'each', gst_treatment: 'exempt', gst_rate_bp: 0, hsn_sac: null,
  default_rate_paise: 50000, rate_is_configured: 1, is_active: 1, notes: null,
}]

async function fakeApi(path: string, options: RequestInit = {}) {
  const method = (options.method as string) || 'GET'
  calls.push({ path, method, body: options.body ? JSON.parse(options.body as string) : null })
  if (path.startsWith('/billing/config')) {
    return {
      module: 'billing', enabled: true, discharge_financial_enforce: false,
      default_tariff_plan: null, service_item_count: 1, priced_service_item_count: 1,
      unpriced_service_item_count: 0, financial_year: '2026-27',
    }
  }
  if (path.startsWith('/billing/service-items')) return { items: ITEMS }
  if (path.startsWith('/billing/tariff-plans')) return { plans: [] }
  if (path.startsWith('/billing/op-patients')) return OP_PATIENTS
  if (path.startsWith('/vigil/patients')) {
    return [{ id: 'P1', name: 'Asha Rao', mrn: 'MRN-1', ward: 'W1', bed: '3', admission_id: 'ADM-1' }]
  }
  if (path.startsWith('/vigil/discharged')) return []
  if (path === '/billing/patients/P9/invoices' && method === 'POST') return { invoice: INVOICE }
  if (path === '/billing/patients/P9/opd-invoice' && method === 'POST') {
    return { invoice: INVOICE, charge_ids: [41, 42] }
  }
  if (path.startsWith('/billing/invoices/31')) return { invoice: INVOICE }
  if (path.startsWith('/billing/invoices')) return { invoices: [] }
  if (path.includes('/discharge-advisory')) {
    if (path.includes('/P9/')) throw new Error('no advisory for a walk-in')
    return {
      ...SUMMARY, patient_id: 'P1', advisory_status: 'clear', unpriceable_event_count: 0,
      uncaptured_event_count: 0, reasons: [], enforced: false, blocks_discharge: false,
      headline: 'No outstanding balance recorded', message: '', checked_at: '2026-10-10T05:00:00',
    }
  }
  if (path.startsWith('/billing/patients/P9/charges')) return CHARGES
  if (path.startsWith('/billing/patients/P1/charges')) {
    return { patient_id: 'P1', charges: [], summary: { ...SUMMARY, patient_id: 'P1' } }
  }
  throw new Error(`unmocked path ${path}`)
}

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options: RequestInit = {}) => fakeApi(path, options),
}))

import BillingPanel from './BillingPanel'

beforeEach(() => {
  calls.length = 0
  resetFeatureFlags()
})

afterEach(() => {
  cleanup()
  resetFeatureFlags()
  vi.clearAllMocks()
})

async function openCharges() {
  fireEvent.click(await screen.findByRole('button', { name: 'Charges' }))
  return (await screen.findByLabelText('Patient')) as HTMLSelectElement
}

describe('outpatients at the billing counter', () => {
  it('asks for nothing new with the feature off', async () => {
    render(<BillingPanel />)
    const picker = await openCharges()
    await waitFor(() => expect(within(picker).getByText(/Asha Rao/)).toBeInTheDocument())
    expect(calls.some(c => c.path.startsWith('/billing/op-patients'))).toBe(false)
    expect(picker.querySelector('optgroup[label="Outpatients today, still to be billed"]')).toBeNull()
  })

  it('lists a walk-in under its own group, and a ward patient keeps the ward entry', async () => {
    setFeatureFlags({ registration: true, opd: true })
    render(<BillingPanel />)
    const picker = await openCharges()
    await waitFor(() => expect(within(picker).getByText(/Ravi Walkin/)).toBeInTheDocument())
    const group = picker.querySelector('optgroup[label="Outpatients today, still to be billed"]')
    expect(group).not.toBeNull()
    expect(within(group as HTMLElement).getByText('Ravi Walkin (UH-9), outpatient')).toBeInTheDocument()
    expect(within(group as HTMLElement).queryByText(/Asha Rao/)).toBeNull()
    expect(within(picker).getAllByText(/Asha Rao/)).toHaveLength(1)
  })

  it('bills this visit over the outpatient lines only, with no admission', async () => {
    setFeatureFlags({ registration: true, opd: true })
    render(<BillingPanel />)
    const picker = await openCharges()
    await waitFor(() => expect(within(picker).getByText(/Ravi Walkin/)).toBeInTheDocument())
    fireEvent.change(picker, { target: { value: 'P9' } })

    const card = await screen.findByTestId('outpatient-visit')
    expect(within(card).getByText('Cardiology, Dr Iyer, token 7')).toBeInTheDocument()
    expect(within(card).getByText(/1 pending charge with no admission is recorded as inpatient/))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Interim bill' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Final bill' })).toBeNull()
    expect(screen.queryByTestId('discharge-advisory')).toBeNull()
    expect(calls.some(c => c.path.includes('/discharge-advisory'))).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Bill this visit' }))
    await waitFor(() => {
      const raised = calls.filter(c => c.path === '/billing/patients/P9/invoices' && c.method === 'POST')
      expect(raised.map(c => c.body)).toEqual([
        { invoice_type: 'final', encounter_type: 'op', charge_ids: [21, 24] },
      ])
    })
    expect(await screen.findByRole('dialog', { name: /Invoice INV-2026-27-000031/ })).toBeInTheDocument()
  })

  it('posts the counter form through the existing counter route', async () => {
    setFeatureFlags({ registration: true, opd: true })
    render(<BillingPanel />)
    const picker = await openCharges()
    await waitFor(() => expect(within(picker).getByText(/Ravi Walkin/)).toBeInTheDocument())
    fireEvent.change(picker, { target: { value: 'P9' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Counter bill' }))

    const dialog = await screen.findByRole('dialog', { name: 'Counter bill: Ravi Walkin' })
    const bill = within(dialog).getByRole('button', { name: 'Bill and issue' })
    expect(bill).toBeDisabled()
    const [first] = within(dialog).getAllByLabelText('Service item')
    fireEvent.change(first, { target: { value: 'CONS-OP' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add another line' }))
    const items = within(dialog).getAllByLabelText('Service item')
    fireEvent.change(items[1], { target: { value: 'CONS-OP' } })
    fireEvent.change(within(dialog).getAllByLabelText('Quantity')[1], { target: { value: '2' } })
    fireEvent.click(bill)

    await waitFor(() => {
      const posted = calls.filter(c => c.path === '/billing/patients/P9/opd-invoice')
      expect(posted.map(c => c.body)).toEqual([{
        lines: [
          { service_code: 'CONS-OP', quantity_milli: 1000 },
          { service_code: 'CONS-OP', quantity_milli: 2000 },
        ],
        issue: true,
      }])
    })
    expect(await screen.findByRole('dialog', { name: /Invoice INV-2026-27-000031/ })).toBeInTheDocument()
  })

  it('refuses a counter line whose quantity is not a number, before sending', async () => {
    setFeatureFlags({ registration: true, opd: true })
    render(<BillingPanel />)
    const picker = await openCharges()
    await waitFor(() => expect(within(picker).getByText(/Ravi Walkin/)).toBeInTheDocument())
    fireEvent.change(picker, { target: { value: 'P9' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Counter bill' }))
    const dialog = await screen.findByRole('dialog', { name: 'Counter bill: Ravi Walkin' })
    fireEvent.change(within(dialog).getByLabelText('Service item'), { target: { value: 'CONS-OP' } })
    fireEvent.change(within(dialog).getByLabelText('Quantity'), { target: { value: 'two' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Bill and issue' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Line 1: quantity must be a number')
    expect(calls.some(c => c.path.endsWith('/opd-invoice'))).toBe(false)
  })

  it('keeps the stay bills for a ward patient', async () => {
    setFeatureFlags({ registration: true, opd: true })
    render(<BillingPanel />)
    const picker = await openCharges()
    await waitFor(() => expect(within(picker).getByText(/Asha Rao/)).toBeInTheDocument())
    fireEvent.change(picker, { target: { value: 'P1' } })
    expect(await screen.findByRole('button', { name: 'Final bill' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Bill this visit' })).toBeNull()
    expect(screen.queryByTestId('outpatient-visit')).toBeNull()
  })
})

describe('the cashier', () => {
  it('still has no Billing tab, whatever the outpatient feature says', () => {
    setFeatureFlags({ registration: true, opd: true })
    expect(getAccessibleTabs('cashier')).not.toContain('billing')
    resetFeatureFlags()
    expect(getAccessibleTabs('cashier')).not.toContain('billing')
  })
})
