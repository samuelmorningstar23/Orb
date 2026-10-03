/**
 * Billing desk, F096: the bill names the hospital, its date and the receipts
 * behind "Paid"; a mis-posted charge can be cancelled with a reason; a charge
 * or bill raised for an inpatient carries the admission (so it is billed as
 * inpatient); a pending charge the stay's bill will not take (no admission)
 * is listed and billed on its own, never dropped; and a discharged patient
 * with something still to bill stays in the picker.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const calls: { path: string; method: string; body: unknown }[] = []

const SUMMARY = {
  patient_id: 'P1', admission_id: 'ADM-1', live_invoice_count: 0, draft_invoice_count: 0,
  outstanding_paise: 0, outstanding_display: '₹0.00',
  unbilled_charge_count: 1, unbilled_total_paise: 50000, unbilled_total_display: '₹500.00',
  unpriced_charge_count: 0,
  deposit_balance_paise: 0, deposit_balance_display: '₹0.00', open_claim_count: 0,
  net_due_paise: 50000, net_due_display: '₹500.00',
}

const CHARGES = {
  patient_id: 'P1',
  charges: [
    { id: 11, admission_id: 'ADM-1', service_code: 'CONS-GEN', description: 'General consultation',
      quantity: 1, unit_rate_paise: 50000, rate_is_configured: 1, total_paise: 50000,
      status: 'pending', source: 'manual', service_date: '2026-10-02' },
    { id: 12, admission_id: 'ADM-1', service_code: 'INV-CBC', description: 'Complete blood count',
      quantity: 1, unit_rate_paise: 30000, rate_is_configured: 1, total_paise: 30000,
      status: 'billed', source: 'manual', service_date: '2026-10-02' },
  ],
  summary: SUMMARY,
}

// The OPD consultation that led to the admission: frontdesk writes it with no
// admission, so the stay's final bill does not sweep it.
const OPD_CHARGE = {
  id: 13, admission_id: null, service_code: 'OPD-CONSULT', description: 'OPD consultation',
  quantity: 1, unit_rate_paise: 80000, rate_is_configured: 1, total_paise: 80000,
  status: 'pending', source: 'frontdesk', service_date: '2026-10-01',
}
let withOpdCharge = false

const INVOICE = {
  id: 7, invoice_number: 'INV-2026-27-000009', number_display: 'INV-2026-27-000009',
  patient_id: 'P1', admission_id: 'ADM-1', encounter_type: 'ip',
  invoice_type: 'final', status: 'partially_paid', is_interstate: 0, is_credit_note: false,
  subtotal_paise: 150000, discount_paise: 0, taxable_value_paise: 150000,
  cgst_paise: 0, sgst_paise: 0, igst_paise: 0, tax_paise: 0, total_paise: 150000,
  rounding_adjustment_paise: 0, rounding_adjustment_display: '₹0.00',
  payable_paise: 150000, payable_display: '₹1,500.00',
  amount_paid_paise: 50000, amount_paid_display: '₹500.00',
  balance_paise: 100000, balance_display: '₹1,000.00',
  supersedes_invoice_id: null, issued_at: '2026-10-02 05:30:00',
  period_from: '2026-09-28', period_to: '2026-10-02',
  supplier_name: 'Sunrise General Hospital',
  cancelled_at: null, cancel_reason: null, notes: null,
  lines: [],
  patient: { id: 'P1', name: 'Asha Rao', mrn: 'MRN-1', age: 44, sex: 'F' },
  payer: null,
  payments: [{ id: 3, receipt_number: 'RCPT-2026-27-000031', amount_display: '₹500.00',
               method: 'upi', status: 'received', direction: 'in', received_at: '2026-10-02 06:00:00' }],
  credit_notes: [], allowed_transitions: [],
}

async function fakeApi(path: string, options: RequestInit = {}) {
  calls.push({
    path,
    method: (options.method as string) || 'GET',
    body: options.body ? JSON.parse(options.body as string) : null,
  })
  if (path.startsWith('/billing/config')) {
    return {
      module: 'billing', enabled: true, discharge_financial_enforce: false,
      default_tariff_plan: null, service_item_count: 0, priced_service_item_count: 0,
      unpriced_service_item_count: 0, financial_year: '2026-27',
    }
  }
  if (path.startsWith('/billing/service-items')) return { items: [] }
  if (path.startsWith('/billing/tariff-plans')) return { plans: [] }
  if (path.startsWith('/billing/charges/11/cancel')) return { status: 'success', charge: { ...CHARGES.charges[0], status: 'cancelled' } }
  if (path.startsWith('/billing/invoices/7')) return { invoice: INVOICE }
  if (path.startsWith('/billing/invoices')) return { invoices: [INVOICE] }
  if (path.startsWith('/billing/patients/P1/invoices')) return { invoice: INVOICE }
  if (path.includes('/charges/capture')) return { patient_id: 'P1', dry_run: true, posted: [], skipped: [], unpriceable_count: 0 }
  if (path.startsWith('/vigil/patients')) {
    return [{ id: 'P1', name: 'Asha Rao', mrn: 'MRN-1', ward: 'W1', bed: '3', admission_id: 'ADM-1' }]
  }
  if (path.startsWith('/vigil/discharged')) {
    return [
      { id: 'P2', name: 'Ravi Kumar', mrn: 'MRN-2', admission_id: 'ADM-2', final_bill_raised: false },
      { id: 'P3', name: 'Meena Iyer', mrn: 'MRN-3', admission_id: 'ADM-3', final_bill_raised: true },
      { id: 'P4', name: 'Kiran Das', mrn: 'MRN-4', admission_id: 'ADM-4', final_bill_raised: true,
        has_pending_charges: true },
    ]
  }
  if (path.includes('/discharge-advisory')) {
    return { ...SUMMARY, advisory_status: 'clear', reasons: [], enforced: false, blocks_discharge: false,
             headline: 'Nothing outstanding', message: 'Advisory only: this does not prevent the discharge.',
             unpriceable_event_count: 0, uncaptured_event_count: 0, checked_at: '2026-10-02T10:00:00' }
  }
  if (path.includes('/charges')) {
    return withOpdCharge ? { ...CHARGES, charges: [...CHARGES.charges, OPD_CHARGE] } : CHARGES
  }
  throw new Error(`unmocked path ${path}`)
}

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options: RequestInit = {}) => fakeApi(path, options),
}))

import BillingPanel from './BillingPanel'

afterEach(() => {
  calls.length = 0
  withOpdCharge = false
  vi.clearAllMocks()
})

async function chooseOnCharges(id: string, name: RegExp) {
  render(<BillingPanel />)
  fireEvent.click(await screen.findByRole('button', { name: 'Charges' }))
  const picker = await screen.findByLabelText('Patient')
  await waitFor(() => expect(within(picker as HTMLElement).getByText(name)).toBeInTheDocument())
  fireEvent.change(picker, { target: { value: id } })
}

describe('billing desk', () => {
  it('cancels a pending charge only with a typed reason', async () => {
    await chooseOnCharges('P1', /Asha Rao/)
    expect(await screen.findByText('General consultation')).toBeInTheDocument()

    // Only the pending row offers it; the billed one does not.
    const offers = screen.getAllByRole('button', { name: 'Cancel charge' })
    expect(offers).toHaveLength(1)
    fireEvent.click(offers[0])

    const confirm = screen.getByRole('button', { name: 'Cancel this charge' })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Reason (required)'), { target: { value: 'Posted to the wrong patient' } })
    await waitFor(() => expect(confirm).toBeEnabled())
    fireEvent.click(confirm)

    await waitFor(() => {
      const sent = calls.find(c => c.path === '/billing/charges/11/cancel')
      expect(sent?.method).toBe('POST')
      expect(sent?.body).toEqual({ reason: 'Posted to the wrong patient' })
    })
  })

  it('raises the bill and pulls charges against the inpatient admission', async () => {
    await chooseOnCharges('P1', /Asha Rao/)
    expect(await screen.findByText('General consultation')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Check the clinical record' }))
    await waitFor(() => {
      const capture = calls.find(c => c.path.includes('/charges/capture'))
      expect(capture?.body).toEqual({ dry_run: true, admission_id: 'ADM-1' })
    })

    fireEvent.click(screen.getByRole('button', { name: 'Final bill' }))
    await waitFor(() => {
      const raised = calls.find(c => c.path === '/billing/patients/P1/invoices' && c.method === 'POST')
      expect(raised?.body).toEqual({ invoice_type: 'final', admission_id: 'ADM-1' })
    })
  })

  it('lists a pending charge with no admission and bills it on its own, never dropping it', async () => {
    withOpdCharge = true
    await chooseOnCharges('P1', /Asha Rao/)
    const other = await screen.findByTestId('other-encounter-charges')
    expect(within(other).getByText('OPD consultation')).toBeInTheDocument()
    expect(other).toHaveTextContent('1 pending charge is not linked to this admission')

    // The stay's bill still sweeps the stay.
    fireEvent.click(screen.getByRole('button', { name: 'Final bill' }))
    await waitFor(() => {
      const raised = calls.filter(c => c.path === '/billing/patients/P1/invoices' && c.method === 'POST')
      expect(raised.map(c => c.body)).toEqual([{ invoice_type: 'final', admission_id: 'ADM-1' }])
    })

    // The leftover charge is billed by id, as outpatient (no admission sent).
    fireEvent.click(within(other).getByRole('button', { name: 'Bill these separately' }))
    await waitFor(() => {
      const raised = calls.filter(c => c.path === '/billing/patients/P1/invoices' && c.method === 'POST')
      expect(raised).toHaveLength(2)
      expect(raised[1].body).toEqual({ invoice_type: 'final', charge_ids: [13] })
    })
  })

  it('with only a charge outside the stay pending, the stay bill buttons are off', async () => {
    withOpdCharge = true
    CHARGES.charges[0].status = 'billed'
    try {
      await chooseOnCharges('P1', /Asha Rao/)
      expect(await screen.findByTestId('other-encounter-charges')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Final bill' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Interim bill' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Bill these separately' })).toBeEnabled()
    } finally {
      CHARGES.charges[0].status = 'pending'
    }
  })

  it('keeps a discharged patient with something still to bill in the picker, and no one else', async () => {
    render(<BillingPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Charges' }))
    const picker = await screen.findByLabelText('Patient')
    expect(await within(picker as HTMLElement).findByText(/Ravi Kumar/)).toBeInTheDocument()
    // Final bill raised, nothing pending: gone. Final bill raised with a
    // charge still pending: kept, or that charge could never be billed here.
    expect(within(picker as HTMLElement).queryByText(/Meena Iyer/)).toBeNull()
    expect(within(picker as HTMLElement).getByText(/Kiran Das/)).toBeInTheDocument()
    expect(within(picker as HTMLElement).getByRole('group', { name: 'Discharged, still to be billed' })).toBeInTheDocument()
  })

  it('heads the bill with the hospital, the bill date, the period and the receipts', async () => {
    render(<BillingPanel />)
    fireEvent.click(await screen.findByText('INV-2026-27-000009'))
    expect(await screen.findByTestId('bill-hospital')).toHaveTextContent('Sunrise General Hospital')
    expect(screen.getByTestId('bill-date').textContent).toMatch(/^Bill date: \d/)
    expect(screen.getByTestId('bill-period').textContent).toMatch(/^Period: .+ to .+/)
    const receipts = screen.getByTestId('bill-receipts')
    expect(within(receipts).getByText(/RCPT-2026-27-000031/)).toBeInTheDocument()
    expect(within(receipts).getByText('₹500.00')).toBeInTheDocument()
  })
})
