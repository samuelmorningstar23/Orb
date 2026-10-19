/**
 * BillingPanel — the four things this screen must never get wrong.
 *
 *   1. AN UNPRICED ITEM READS AS WORDS. "Not configured", never "₹0.00". A zero
 *      on a price list is a free item, and somebody will bill it. The fixture is
 *      built to make this fail loudly if the code trusts the wrong field: the
 *      unpriced item's `default_rate_paise` IS 0 in the database, and only
 *      `rate_is_configured` distinguishes it from a genuinely free service.
 *   2. THE DISCHARGE ADVISORY SAYS IT DOES NOT BLOCK. In the card, in words, on
 *      the same screen as the amount — not in a tooltip and not implied.
 *   3. MONEY LEAVES THE BROWSER AS INTEGER PAISE. "1,234.55" is sent as 123455,
 *      and a BLANK price is sent as null rather than 0.
 *   4. A CANCELLATION CANNOT BE CLICKED WITHOUT A REASON. The server refuses a
 *      blank one; the button must not offer it.
 *
 * The API client is mocked at `apiFetch`, so src/api/billing.ts — its URLs, its
 * request bodies, its types — is exercised for real by these tests rather than
 * stubbed over.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const calls: { path: string; method: string; body: Record<string, unknown> | null }[] = []
let overrides: Record<string, unknown> = {}

const PRICED_ITEM = {
  id: 1, code: 'CONS-GEN', name: 'General consultation', category: 'consultation',
  department: null, uom: 'each', gst_treatment: 'exempt', gst_rate_bp: 0,
  hsn_sac: null, default_rate_paise: 50000, rate_is_configured: 1, is_active: 1,
  notes: null,
}

/** The dangerous one: no price set, and a 0 sitting in the amount column. */
const UNPRICED_ITEM = {
  ...PRICED_ITEM, id: 2, code: 'PROC-NEW', name: 'New procedure',
  category: 'procedure', default_rate_paise: 0, rate_is_configured: 0,
}

const INVOICE = {
  id: 7, invoice_number: 'INV-2026-27-000004', number_display: 'INV-2026-27-000004',
  patient_id: 'P1', patient_name: 'Asha Rao', admission_id: null, encounter_type: 'ip',
  invoice_type: 'final', status: 'issued', is_interstate: 0, is_credit_note: false,
  subtotal_paise: 150000, discount_paise: 0, taxable_value_paise: 150000,
  cgst_paise: 0, sgst_paise: 0, igst_paise: 0, tax_paise: 0, total_paise: 150000,
  rounding_adjustment_paise: 0, rounding_adjustment_display: '₹0.00',
  payable_paise: 150000, payable_display: '₹1,500.00',
  amount_paid_paise: 0, amount_paid_display: '₹0.00',
  balance_paise: 150000, balance_display: '₹1,500.00',
  supersedes_invoice_id: null, issued_at: '2026-08-12T10:00:00', cancelled_at: null,
  cancel_reason: null, notes: null,
  lines: [{
    id: 1, line_no: 1, encounter_charge_id: 11, service_code: 'CONS-GEN',
    description: 'General consultation', hsn_sac: null, quantity: 1, uom: 'each',
    unit_rate_paise: 150000, unit_rate_display: '₹1,500.00',
    gross_paise: 150000, gross_display: '₹1,500.00', discount_paise: 0,
    gst_treatment: 'exempt', gst_rate_bp: 0, taxable_value_paise: 150000,
    cgst_paise: 0, sgst_paise: 0, igst_paise: 0, tax_paise: 0, tax_display: '₹0.00',
    total_paise: 150000, total_display: '₹1,500.00',
  }],
  patient: { id: 'P1', name: 'Asha Rao', mrn: 'MRN-1', age: 44, sex: 'F' },
  payer: null, payments: [], credit_notes: [], allowed_transitions: ['paid', 'cancelled'],
}

const SUMMARY = {
  patient_id: 'P1', admission_id: null, live_invoice_count: 1, draft_invoice_count: 0,
  outstanding_paise: 150000, outstanding_display: '₹1,500.00',
  unbilled_charge_count: 0, unbilled_total_paise: 0, unbilled_total_display: '₹0.00',
  unpriced_charge_count: 0,
  deposit_balance_paise: 0, deposit_balance_display: '₹0.00', open_claim_count: 0,
  net_due_paise: 150000, net_due_display: '₹1,500.00',
}

const ADVISORY = {
  ...SUMMARY,
  advisory_status: 'outstanding', unpriceable_event_count: 0, uncaptured_event_count: 0,
  reasons: ['₹1,500.00 is outstanding on issued invoices'],
  /** The pilot setting, and the invariant this whole module is built around. */
  enforced: false,
  blocks_discharge: false,
  headline: 'Outstanding: ₹1,500.00',
  message: 'Advisory only: this does not prevent the discharge.',
  checked_at: '2026-08-12T10:00:00',
}

/** One fake server for the whole file; `overrides` swaps a single response. */
async function fakeApi(path: string, options: RequestInit = {}) {
  calls.push({
    path,
    method: (options.method as string) || 'GET',
    body: options.body ? JSON.parse(options.body as string) : null,
  })
  for (const [prefix, value] of Object.entries(overrides)) {
    if (path.startsWith(prefix)) return value
  }
  if (path.startsWith('/billing/config')) {
    return {
      module: 'billing', enabled: true, discharge_financial_enforce: false,
      default_tariff_plan: null, service_item_count: 2,
      priced_service_item_count: 1, unpriced_service_item_count: 1,
      financial_year: '2026-27',
    }
  }
  if (path.startsWith('/billing/service-items')) {
    return options.method === 'POST'
      ? { item: PRICED_ITEM }
      : { items: [PRICED_ITEM, UNPRICED_ITEM] }
  }
  if (path.startsWith('/billing/tariff-plans')) {
    return {
      plans: [{
        id: 1, code: 'SELFPAY', name: 'Self-pay', plan_type: 'self_pay',
        is_default: 1, is_active: 1,
      }],
    }
  }
  if (path.startsWith('/billing/invoices/7')) return { invoice: INVOICE }
  if (path.startsWith('/billing/invoices')) return { invoices: [INVOICE] }
  if (path.startsWith('/vigil/patients')) {
    return [{ id: 'P1', name: 'Asha Rao', mrn: 'MRN-1', ward: 'W1', bed: '3' }]
  }
  if (path.includes('/discharge-advisory')) return ADVISORY
  if (path.includes('/charges')) return { patient_id: 'P1', charges: [], summary: SUMMARY }
  throw new Error(`unmocked path ${path}`)
}

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options: RequestInit = {}) => fakeApi(path, options),
}))

import BillingPanel from './BillingPanel'
import { formatClinicalDate } from '../../lib/datetime'

function setup(withOverrides: Record<string, unknown> = {}) {
  calls.length = 0
  overrides = withOverrides
  return render(<BillingPanel />)
}

const click = (el: Element) => fireEvent.click(el)
const type = (el: Element, value: string) =>
  fireEvent.change(el, { target: { value } })

afterEach(() => {
  overrides = {}
  vi.clearAllMocks()
})

async function openTab(name: string) {
  click(await screen.findByRole('button', { name }))
}

async function selectPatient() {
  await openTab('Charges')
  const picker = await screen.findByLabelText('Patient')
  await waitFor(() => expect(within(picker as HTMLElement).getByText(/Asha Rao/)).toBeInTheDocument())
  fireEvent.change(picker, { target: { value: 'P1' } })
}

/* ── 1. Not configured is not zero ───────────────────────────────────────── */

describe('the price list', () => {
  it('renders an unpriced item as words, never as ₹0.00', async () => {
    setup()
    await openTab('Price list')

    const unpricedRow = (await screen.findByText('New procedure')).closest('tr')!
    expect(within(unpricedRow).getByText('Not configured')).toBeInTheDocument()
    expect(within(unpricedRow).queryByText('₹0.00')).toBeNull()

    // …while an item that IS priced shows its rupees.
    const pricedRow = screen.getByText('General consultation').closest('tr')!
    expect(within(pricedRow).getByText('₹500.00')).toBeInTheDocument()
  })

  it('warns that unpriced items cannot be billed', async () => {
    setup()
    await openTab('Price list')
    expect(await screen.findByText(/1 item has no price/)).toBeInTheDocument()
  })

  /**
   * THE CAPTURED-CHARGE ROW, which broke rule 1 on its way in from the OPD.
   *
   * Completing an OPD visit always writes a charge — the consultation happened
   * — and writes it with `rate_is_configured = 0` and `total_paise = 0` when
   * nobody has priced the item (frontdesk_service: "UNPRICED IS NOT FREE"). The
   * amount cell rendered that 0 as "₹0.00" while the rate cell one column left
   * already read "Not configured": the same row said both "nobody has priced
   * this" and "this costs nothing", and the second one is what gets billed.
   */
  it('renders an unpriced CAPTURED CHARGE as words, never as ₹0.00', async () => {
    setup({
      '/billing/patients/P1/charges': {
        patient_id: 'P1',
        charges: [
          { id: 1, service_code: 'OPD-CONSULT', description: 'General Medicine consultation',
            quantity: 1, unit_rate_paise: 0, rate_is_configured: 0, total_paise: 0,
            status: 'pending', source: 'manual',
            service_date: '2026-08-12T06:20:45.408698+00:00' },
          { id: 2, service_code: 'CONS-OP', description: 'OPD consultation',
            quantity: 2, unit_rate_paise: 80000, rate_is_configured: 1, total_paise: 160000,
            status: 'pending', source: 'manual', service_date: '2026-08-12' },
          // 20:00 UTC is 01:30 the next morning in IST: the visit's local day,
          // not the UTC date a slice would print.
          { id: 3, service_code: 'OPD-CONSULT', description: 'Late OPD consultation',
            quantity: 1, unit_rate_paise: 80000, rate_is_configured: 1, total_paise: 80000,
            status: 'pending', source: 'manual',
            service_date: '2026-08-12T20:00:00+00:00' },
        ],
        summary: SUMMARY,
      },
    })
    await selectPatient()

    const unpriced = (await screen.findByText('General Medicine consultation')).closest('tr')!
    expect(within(unpriced).queryByText('₹0.00')).toBeNull()
    expect(within(unpriced).getAllByText('Not configured').length).toBeGreaterThanOrEqual(1)
    // A charge date is a DAY on a bill. The OPD writer's full timestamp and the
    // manual writer's '2026-08-12' print in the same clinical form.
    expect(within(unpriced).getByText('12 Aug 2026')).toBeInTheDocument()
    const late = screen.getByText('Late OPD consultation').closest('tr')!
    expect(within(late).getByText(formatClinicalDate(new Date('2026-08-12T20:00:00Z'))))
      .toBeInTheDocument()
    expect(within(late).queryByText('2026-08-12')).toBeNull()

    // …while a priced charge still shows its rupees, on both cells.
    const priced = screen.getByText('OPD consultation').closest('tr')!
    expect(within(priced).getByText('₹1,600.00')).toBeInTheDocument()
  })
})

/* ── 1b. The card above the rows obeys the same rule ─────────────────────── */

describe('the "Captured, not yet billed" card', () => {
  /**
   * The card total is `unbilled_total_paise`, which an unpriced charge sits in
   * as a 0 that is not a price. Fixing the ROW (above) while the CARD one
   * viewport-inch higher still said "₹0.00" left the panel telling both truths
   * at once — and the card is the one a clerk reads first.
   */
  it('reads as words, never ₹0.00, when every pending charge is unpriced', async () => {
    setup({
      '/billing/patients/P1/charges': {
        patient_id: 'P1',
        charges: [
          { id: 1, service_code: 'OPD-CONSULT', description: 'General Medicine consultation',
            quantity: 1, unit_rate_paise: 0, rate_is_configured: 0, total_paise: 0,
            status: 'pending', source: 'manual', service_date: '2026-08-12' },
        ],
        summary: {
          ...SUMMARY,
          outstanding_paise: 0, outstanding_display: '₹0.00',
          unbilled_charge_count: 1, unpriced_charge_count: 1,
          net_due_paise: 0, net_due_display: '₹0.00',
        },
      },
    })
    await selectPatient()

    const card = (await screen.findByText(/Captured, not yet billed/))
      .closest('.bill-metric')! as HTMLElement
    expect(within(card).queryByText('₹0.00')).toBeNull()
    expect(within(card).getByText('Not configured')).toBeInTheDocument()
    expect(within(card).getByText(/1 unpriced/)).toBeInTheDocument()
  })

  it('shows a partly-priced total but says it is incomplete', async () => {
    setup({
      '/billing/patients/P1/charges': {
        patient_id: 'P1',
        charges: [],
        summary: {
          ...SUMMARY,
          unbilled_charge_count: 3, unpriced_charge_count: 1,
          unbilled_total_paise: 80000, unbilled_total_display: '₹800.00',
        },
      },
    })
    await selectPatient()

    const card = (await screen.findByText(/Captured, not yet billed/))
      .closest('.bill-metric')! as HTMLElement
    // The priced portion is a real figure and stays visible…
    expect(within(card).getByText('₹800.00')).toBeInTheDocument()
    // …but never as the whole story.
    expect(within(card).getByText(/1 unpriced: this total is incomplete/)).toBeInTheDocument()
  })
})

/* ── 2. Money leaves the browser as integer paise ────────────────────────── */

describe('money entry', () => {
  it('sends a typed rupee amount as integer paise, never a float', async () => {
    setup()
    await openTab('Price list')
    click(await screen.findByRole('button', { name: 'New item' }))

    type(screen.getByLabelText('Code'), 'PKG-1')
    type(screen.getByLabelText('Name'), 'Package')
    type(screen.getByLabelText('Default rate (optional)'), '1,234.55')

    // The field echoes what it PARSED, so a typo is visible before it is saved.
    expect(await screen.findByText('₹1,234.55')).toBeInTheDocument()

    click(screen.getByRole('button', { name: 'Save item' }))
    await waitFor(() => {
      expect(calls.some(c => c.path === '/billing/service-items' && c.method === 'POST'))
        .toBe(true)
    })
    const post = calls.find(c => c.path === '/billing/service-items' && c.method === 'POST')!
    expect(post.body?.default_rate_paise).toBe(123455)
    expect(Number.isInteger(post.body?.default_rate_paise)).toBe(true)
  })

  it('sends a BLANK price as null, not as zero', async () => {
    setup()
    await openTab('Price list')
    click(await screen.findByRole('button', { name: 'New item' }))
    type(screen.getByLabelText('Code'), 'PROC-2')
    type(screen.getByLabelText('Name'), 'Unpriced procedure')
    click(screen.getByRole('button', { name: 'Save item' }))

    await waitFor(() => {
      expect(calls.some(c => c.path === '/billing/service-items' && c.method === 'POST'))
        .toBe(true)
    })
    const post = calls.find(c => c.path === '/billing/service-items' && c.method === 'POST')!
    expect(post.body?.default_rate_paise).toBeNull()
    // A new item never overwrites a code already on the list (F098): the blank
    // price above used to un-price an existing code.
    expect(post.body?.create_only).toBe(true)
  })

  it('refuses a sub-paise amount instead of silently rounding it', async () => {
    setup()
    await openTab('Price list')
    click(await screen.findByRole('button', { name: 'New item' }))
    type(screen.getByLabelText('Default rate (optional)'), '10.005')
    expect(await screen.findByRole('alert')).toHaveTextContent(/finer than one paise/)
  })
})

/* ── 3. The discharge advisory ───────────────────────────────────────────── */

describe('the discharge advisory', () => {
  it('shows the amount AND says in words that it does not block the discharge', async () => {
    setup()
    await selectPatient()

    const card = await screen.findByTestId('discharge-advisory')
    expect(within(card).getByText('Outstanding: ₹1,500.00')).toBeInTheDocument()
    expect(
      within(card).getByText('Advisory only: this does not prevent the discharge.'),
    ).toBeInTheDocument()
    expect(within(card).queryByText(/override/i)).toBeNull()
  })

  it('says so plainly when a site has switched enforcement ON', async () => {
    setup({
      '/billing/patients/P1/discharge-advisory': {
        ...ADVISORY, enforced: true, blocks_discharge: true,
      },
    })
    await selectPatient()

    const card = await screen.findByTestId('discharge-advisory')
    expect(within(card).getByText(/enforces financial clearance/)).toBeInTheDocument()
    expect(within(card).getByText(/override with a typed reason/)).toBeInTheDocument()
  })

  it('reports an unknown position as unknown, not as ₹0 owed', async () => {
    setup({
      '/billing/patients/P1/discharge-advisory': {
        ...ADVISORY,
        advisory_status: 'unknown',
        outstanding_paise: 0,
        outstanding_display: '₹0.00',
        net_due_paise: 0,
        unpriceable_event_count: 2,
        headline: 'Financial position: NOT KNOWN — this bill is incomplete',
        reasons: ['2 clinical event(s) could not be priced and are missing from this bill'],
      },
    })
    await selectPatient()

    const card = await screen.findByTestId('discharge-advisory')
    expect(within(card).getByText(/NOT KNOWN/)).toBeInTheDocument()
    expect(within(card).getByText(/could not be priced/)).toBeInTheDocument()
    expect(
      within(card).getByText('Advisory only: this does not prevent the discharge.'),
    ).toBeInTheDocument()
  })
})

/* ── 4. Cancellation needs a reason, and the modal is a real dialog ──────── */

describe('an issued invoice', () => {
  it('will not offer cancellation until a reason is typed', async () => {
    setup()
    click(await screen.findByText('INV-2026-27-000004'))

    const cancel = await screen.findByRole('button', { name: 'Cancel invoice' })
    expect(cancel).toBeDisabled()

    type(screen.getByLabelText(/Reason \(required/), 'Wrong patient')
    await waitFor(() => expect(cancel).toBeEnabled())
  })

  it('portals its dialog to document.body so the scrim covers the whole chrome', async () => {
    const { container } = setup()
    click(await screen.findByText('INV-2026-27-000004'))

    const dialog = await screen.findByRole('dialog')
    expect(container.contains(dialog)).toBe(false)
    expect(document.body.contains(dialog)).toBe(true)
  })

  it('prints: the dialog is a print reveal root and the button stays off the paper', async () => {
    setup({
      '/billing/invoices/7/print': {
        invoice: INVOICE,
        supplier: {
          name: 'Orb General', name_is_configured: true, address: 'Not configured',
          gstin: 'Not configured',
          notice: 'This document is a BILL, not a GST tax invoice.',
        },
        number_series_configured: true, number_notice: null,
        amount_in_words: 'Rupees One Thousand Five Hundred Only', has_tax: false,
        gst_note: 'Healthcare services are exempt.', printed_at: '2026-08-12T10:05:00+05:30',
      },
    })
    click(await screen.findByText('INV-2026-27-000004'))
    const dialog = await screen.findByRole('dialog')
    // src/print.css hides every element at print time except the named
    // layouts; without this class "Print this bill" printed a blank page.
    expect(dialog.classList.contains('bill-print-layout')).toBe(true)

    click(screen.getByRole('button', { name: 'Print view' }))
    const supplier = await screen.findByTestId('bill-supplier')
    expect(within(supplier).getByText('Orb General')).toBeInTheDocument()
    expect(within(supplier).getAllByText('Not configured')).toHaveLength(2)
    const printBtn = screen.getByRole('button', { name: 'Print this bill' })
    expect(printBtn.closest('.bill-actions')).not.toBeNull()
  })

  it('prints no "not a tax invoice" notice once the bill carries a GSTIN and address', async () => {
    setup({
      '/billing/invoices/7/print': {
        invoice: INVOICE,
        supplier: {
          name: 'Orb Hospitals Pvt Ltd', name_is_configured: true,
          address: '12 MG Road, Pune, Maharashtra 411001', gstin: '27AAPFU0939F1ZV',
          state_code: '27', source: 'snapshot', notice: null,
        },
        number_series_configured: true, number_notice: null,
        amount_in_words: 'Rupees One Thousand Five Hundred Only', has_tax: false,
        gst_note: 'Healthcare services are exempt.', printed_at: '2026-08-12T10:05:00+05:30',
      },
    })
    click(await screen.findByText('INV-2026-27-000004'))
    click(await screen.findByRole('button', { name: 'Print view' }))
    const supplier = await screen.findByTestId('bill-supplier')
    expect(within(supplier).getByText('27AAPFU0939F1ZV')).toBeInTheDocument()
    const printView = screen.getByTestId('print-view')
    expect(printView.querySelectorAll('.bill-notice--warn')).toHaveLength(0)
    expect(printView.textContent).not.toMatch(/not a GST tax invoice/)
  })

  it('shows an exempt line as GST-exempt healthcare, not as 0% tax', async () => {
    setup()
    click(await screen.findByText('INV-2026-27-000004'))
    const doc = await screen.findByTestId('invoice-document')
    expect(within(doc).getByText(/GST exempt \(healthcare\)/)).toBeInTheDocument()
  })
})

/* ── 5. The selected patient survives a tab round trip ───────────────────── */

/**
 * A CLERK CAPTURING CHARGES DOES NOT LOSE THEIR PLACE FOR LOOKING AT A BILL.
 *
 * The panel renders exactly one tab at a time, so stepping over to Bills
 * UNMOUNTS ChargesTab. While the chosen patient was ChargesTab's own state it
 * died with the unmount: coming back, the picker read "Choose a patient…" and
 * the running bill was gone — every single time, mid-way through a ward's
 * worth of charge entry. The selection belongs to the panel, not to the tab.
 */
const THREE_CHARGES = {
  patient_id: 'P1',
  charges: [
    { id: 1, service_code: 'CONS-GEN', description: 'General consultation',
      quantity: 1, unit_rate_paise: 50000, rate_is_configured: 1, total_paise: 50000,
      status: 'pending', source: 'manual', service_date: '2026-08-12' },
    { id: 2, service_code: 'INV-CBC', description: 'Complete blood count',
      quantity: 1, unit_rate_paise: 30000, rate_is_configured: 1, total_paise: 30000,
      status: 'pending', source: 'manual', service_date: '2026-08-12' },
    { id: 3, service_code: 'BED-GEN', description: 'General ward bed day',
      quantity: 2, unit_rate_paise: 20000, rate_is_configured: 1, total_paise: 40000,
      status: 'pending', source: 'manual', service_date: '2026-08-12' },
  ],
  summary: SUMMARY,
}

describe('the charges tab', () => {
  it('still has the patient selected after a trip to Bills and back', async () => {
    setup({ '/billing/patients/P1/charges': THREE_CHARGES })
    await selectPatient()

    // Where the clerk got to: a patient chosen and their three charges loaded.
    const picker = (await screen.findByLabelText('Patient')) as HTMLSelectElement
    expect(picker.value).toBe('P1')
    expect(await screen.findByText('General consultation')).toBeInTheDocument()
    expect(screen.getByText('Complete blood count')).toBeInTheDocument()
    expect(screen.getByText('General ward bed day')).toBeInTheDocument()

    // Check a bill — the round trip that used to wipe the selection.
    await openTab('Bills')
    expect(await screen.findByText('INV-2026-27-000004')).toBeInTheDocument()
    expect(screen.queryByLabelText('Patient')).toBeNull()   // the tab really unmounted

    await openTab('Charges')

    const returned = (await screen.findByLabelText('Patient')) as HTMLSelectElement
    expect(returned.value).toBe('P1')
    expect(screen.queryByText('Choose a patient to see their running bill.')).toBeNull()
    // …and the running bill is back with it, not left empty.
    expect(await screen.findByText('General consultation')).toBeInTheDocument()
    expect(screen.getByText('Complete blood count')).toBeInTheDocument()
    expect(screen.getByText('General ward bed day')).toBeInTheDocument()
  })

  it('starts with no patient chosen, and says so', async () => {
    setup({ '/billing/patients/P1/charges': THREE_CHARGES })
    await openTab('Charges')

    const picker = (await screen.findByLabelText('Patient')) as HTMLSelectElement
    expect(picker.value).toBe('')
    expect(screen.getByText('Choose a patient to see their running bill.')).toBeInTheDocument()
  })
})
