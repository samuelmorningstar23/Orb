/**
 * PaymentsPanel — the counter screen's honesty guarantees.
 *
 * These are not "does it render" tests. Each one holds shut a way this screen
 * could tell a clerk something untrue about money:
 *
 *   • an uncounted drawer rendered as ₹0.00 (an empty till and an uncounted till
 *     are opposite facts);
 *   • a day-end headline that does not equal its own breakdown (this repo has
 *     been burned by exactly that, and the clerk trusts the headline);
 *   • an editable receipt (there is no such thing — the only correction is a
 *     reversal, and the reason is mandatory);
 *   • an over-payment described as a credit on the bill rather than as an
 *     advance the hospital is holding;
 *   • a UPI/card payment accepted with no reference, which cannot be matched to
 *     the bank statement afterwards;
 *   • a typed amount going through parseFloat and landing a fraction of a paise
 *     off;
 *   • money applied to an invoice the clerk never saw, which one mistyped
 *     digit makes another patient's bill;
 *   • money taken for a patient ID typed by hand, which can name nobody.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PaymentsPanel from './PaymentsPanel'
import { formatClinicalDateTime } from '../../lib/datetime'
import type { CounterPatient, DayEnd, MethodRow, OpenInvoice, Receipt } from '../../api/payments'

/* The API module is mocked wholesale: this suite is about what the SCREEN does
   with a server answer, and the server's own arithmetic is pinned by
   server/tests/test_payments.py. `needsReference` and the method list are the
   real implementations — they are pure data the screen must honour. */
const api = vi.hoisted(() => ({
  listCounters: vi.fn(),
  getCounterExpected: vi.fn(),
  openCounter: vi.fn(),
  closeCounter: vi.fn(),
  recordPayment: vi.fn(),
  listReceipts: vi.fn(),
  reverseReceipt: vi.fn(),
  listRefunds: vi.fn(),
  listDiscounts: vi.fn(),
  decideRefund: vi.fn(),
  decideDiscount: vi.fn(),
  payRefund: vi.fn(),
  getDayEnd: vi.fn(),
  searchCounterPatients: vi.fn(),
  listOpenInvoices: vi.fn(),
}))

vi.mock('../../api/payments', async () => {
  const actual = await vi.importActual<typeof import('../../api/payments')>('../../api/payments')
  return {
    ...actual,
    ...api,
  }
})

const methodRow = (over: Partial<MethodRow> & { method: MethodRow['method'] }): MethodRow => ({
  in_paise: 0, out_paise: 0, net_paise: 0, in_count: 0, out_count: 0,
  in_display: '₹0.00', out_display: '₹0.00', net_display: '₹0.00',
  ...over,
})

const receipt = (over: Partial<Receipt> = {}): Receipt => ({
  id: 1,
  receipt_number: 'RCPT-2026-27-000001',
  patient_id: 'P001',
  admission_id: null,
  invoice_id: null,
  payer_id: null,
  direction: 'in',
  amount_paise: 250000,
  amount_display: '₹2,500.00',
  signed_paise: 250000,
  method: 'cash',
  reference_no: null,
  bank_name: null,
  instrument_date: null,
  purpose: 'deposit',
  status: 'received',
  counter_session_id: null,
  refund_id: null,
  reversal_of_payment_id: null,
  received_by: 'ADM1',
  received_at: '2026-08-12 09:00:00',
  cancelled_by: null,
  cancelled_at: null,
  cancel_reason: null,
  notes: null,
  is_reversed: false,
  is_immutable: true,
  ...over,
})

const dayEnd = (over: Partial<DayEnd> = {}): DayEnd => ({
  day: '2026-08-12',
  window_utc: { from: '2026-08-11 18:30:00', to: '2026-08-12 18:30:00' },
  counter_code: null,
  by_method: [
    methodRow({ method: 'cash', in_paise: 500000, net_paise: 500000, in_count: 3, in_display: '₹5,000.00', net_display: '₹5,000.00' }),
    methodRow({ method: 'upi', in_paise: 123456, net_paise: 123456, in_count: 2, in_display: '₹1,234.56', net_display: '₹1,234.56' }),
  ],
  totals: {
    in_paise: 623456, out_paise: 0, net_paise: 623456, in_count: 5, out_count: 0,
    in_display: '₹6,234.56', out_display: '₹0.00', net_display: '₹6,234.56',
  },
  cash_in_paise: 500000,
  cash_out_paise: 0,
  cash_net_paise: 500000,
  cash_net_display: '₹5,000.00',
  sessions: [],
  counted_cash_paise: null,
  counted_cash_display: 'Not counted',
  variance_paise: null,
  variance_display: 'Not counted',
  open_session_count: 1,
  is_final: false,
  open_refund_count: 0,
  open_discount_count: 0,
  receipt_series_is_configured: true,
  ...over,
})

beforeEach(() => {
  api.listCounters.mockResolvedValue([])
  api.getCounterExpected.mockResolvedValue(null)
  api.listReceipts.mockResolvedValue([])
  api.listRefunds.mockResolvedValue([])
  api.listDiscounts.mockResolvedValue([])
  api.getDayEnd.mockResolvedValue(dayEnd())
  api.searchCounterPatients.mockResolvedValue([])
  api.listOpenInvoices.mockResolvedValue([])
  // The offline write guard reads navigator.onLine.
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true)
})

afterEach(() => {
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

/* `@testing-library/user-event` is not installed in this repo. Every interaction
   below goes through fireEvent inside `act`, so the promises a click starts have
   settled before the assertion runs. */
async function click(el: HTMLElement): Promise<void> {
  await act(async () => { fireEvent.click(el) })
}

async function type(el: HTMLElement, value: string): Promise<void> {
  await act(async () => { fireEvent.change(el, { target: { value } }) })
}

async function select(el: HTMLElement, value: string): Promise<void> {
  await act(async () => { fireEvent.change(el, { target: { value } }) })
}

async function openTab(name: RegExp) {
  render(<PaymentsPanel />)
  await click(screen.getByRole('tab', { name }))
}

const ravi: CounterPatient = {
  id: 'P001', name: 'Ravi Sharma', mrn: 'MRN-GM001', bed: 'GM-101', ward: 'GM-1',
  status: 'admitted',
}

/** The patient is FOUND, never typed: search by name, then pick the option. */
async function choosePatient(p: CounterPatient = ravi): Promise<void> {
  api.searchCounterPatients.mockResolvedValue([p])
  await type(screen.getByRole('combobox', { name: /patient/i }), p.name.slice(0, 4))
  await click(await screen.findByRole('option', { name: new RegExp(p.name) }))
}

/* ══ Day end ═════════════════════════════════════════════════════════════ */

describe('day-end sheet', () => {
  it('shows the total and every method row it is the sum of', async () => {
    await openTab(/day end/i)
    // The footer is the sum of the body rows: 5,000.00 + 1,234.56 = 6,234.56.
    const cashRow = (await screen.findByText('Cash')).closest('tr')!
    expect(within(cashRow).getAllByText('₹5,000.00').length).toBeGreaterThan(0)
    const upiRow = screen.getByText('UPI').closest('tr')!
    expect(within(upiRow).getAllByText('₹1,234.56').length).toBeGreaterThan(0)
    const totalRow = screen.getByText('Total').closest('tr')!
    expect(within(totalRow).getAllByText('₹6,234.56').length).toBeGreaterThan(0)
    expect(500000 + 123456).toBe(623456)
  })

  it('warns loudly when the headline does not equal its own breakdown', async () => {
    // THE FAILURE THIS HOLDS SHUT: a total computed by one query and a breakdown
    // by another, disagreeing, with the clerk trusting the big number.
    api.getDayEnd.mockResolvedValue(dayEnd({
      totals: {
        in_paise: 999999, out_paise: 0, net_paise: 999999, in_count: 5, out_count: 0,
        in_display: '₹9,999.99', out_display: '₹0.00', net_display: '₹9,999.99',
      },
    }))
    await openTab(/day end/i)
    const alert = await screen.findByText(/does not add up/i)
    expect(alert).toBeInTheDocument()
    expect(alert.textContent).toMatch(/Do not sign it/i)
  })

  it('renders an uncounted drawer as "Not counted" and never as a zero amount', async () => {
    await openTab(/day end/i)
    await screen.findByText('Total')
    const counted = screen.getByText('Counted').closest('div')!
    expect(within(counted).getByText('Not counted')).toBeInTheDocument()
    expect(within(counted).queryByText('₹0.00')).toBeNull()

    const variance = screen.getByText('Variance').closest('div')!
    expect(within(variance).getByText('Not counted')).toBeInTheDocument()
  })

  it('says the day is not final while a counter is still open', async () => {
    await openTab(/day end/i)
    expect(await screen.findByText(/still open/i)).toBeInTheDocument()
    expect(screen.getByText(/not counted yet/i)).toBeInTheDocument()
  })

  /* THE FAILURE: a till opened yesterday and never closed was not on today's
     sheet at all, so the sheet read final with "Counted ₹0.00". The server now
     holds the day open and names the drawer; the screen must show that name. */
  it('names a till left open since an earlier day, and who opened it', async () => {
    api.getDayEnd.mockResolvedValue(dayEnd({
      finality_blockers: [
        '1 counter session(s) are still open and uncounted',
        'Counter OPD-1 has been open since 1 Oct 2026, 09:00 IST (opened by ADM1), '
          + 'before this day began. Count it and close it',
      ],
    }))
    await openTab(/day end/i)
    const box = await screen.findByTestId('dayend-blockers')
    expect(box).toHaveTextContent(/not counted yet/i)
    expect(box).toHaveTextContent('Counter OPD-1 has been open since 1 Oct 2026')
    expect(box).toHaveTextContent('opened by ADM1')
  })

  it('does not claim a counter is open when only cash in no drawer holds the day', async () => {
    api.getDayEnd.mockResolvedValue(dayEnd({
      open_session_count: 0,
      finality_blockers: [
        '2 cash receipt(s) totalling ₹700.00 were taken with no counter session. That money '
          + "is in this day's takings and in no drawer, so the day's variance cannot be attributed",
      ],
    }))
    await openTab(/day end/i)
    const box = await screen.findByTestId('dayend-blockers')
    expect(box).toHaveTextContent(/no counter session/i)
    expect(screen.queryByText(/counters? still open/i)).toBeNull()
  })

  it('flags a placeholder receipt series rather than printing it as official', async () => {
    api.getDayEnd.mockResolvedValue(dayEnd({ receipt_series_is_configured: false }))
    await openTab(/day end/i)
    expect(await screen.findByText(/receipt series is not configured/i)).toBeInTheDocument()
    expect(screen.getByText(/placeholders/i)).toBeInTheDocument()
  })
})

/* ══ Receipts ════════════════════════════════════════════════════════════ */

describe('receipts', () => {
  it('offers no way to edit a receipt — only to reverse it', async () => {
    api.listReceipts.mockResolvedValue([receipt()])
    await openTab(/receipts/i)
    const row = (await screen.findByText('RCPT-2026-27-000001')).closest('tr')!
    // The row itself states the rule; the caption states it again for the sheet.
    expect(within(row).getByText(/cannot be edited/i)).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: /^edit$/i })).toBeNull()
    expect(within(row).getByRole('button', { name: /reverse/i })).toBeInTheDocument()
  })

  it('names the patient and who took it, and says when it was received', async () => {
    api.listReceipts.mockResolvedValue([receipt({
      patient_id: 'PAT-50A2D09E', patient_name: 'Ravi Sharma', patient_mrn: 'MRN-GM001',
      received_by: 'ADM1', received_by_name: 'Asha Clerk',
    })])
    await openTab(/receipts/i)
    expect(screen.getByRole('columnheader', { name: 'Received' })).toBeInTheDocument()
    const row = (await screen.findByText('RCPT-2026-27-000001')).closest('tr')!
    expect(row).toHaveTextContent('Ravi Sharma')
    expect(row).toHaveTextContent('MRN-GM001')
    expect(row).toHaveTextContent('Asha Clerk')
    expect(row).not.toHaveTextContent('PAT-50A2D09E')
    expect(row).not.toHaveTextContent('ADM1')
    // '2026-08-12 09:00:00' is UTC; the column shows it in local time.
    expect(row).toHaveTextContent(formatClinicalDateTime(new Date(Date.UTC(2026, 7, 12, 9, 0))))
  })

  it('refuses to reverse without a reason, then reverses with one', async () => {
    api.listReceipts.mockResolvedValue([receipt()])
    api.reverseReceipt.mockResolvedValue({
      original: receipt({ status: 'reversed', is_reversed: true }),
      reversal: receipt({ id: 2, direction: 'out', receipt_number: 'RCPT-2026-27-000002' }),
      invoices_recomputed: [],
      advance_withdrawn_paise: 0,
    })
    await openTab(/receipts/i)
    await click(await screen.findByRole('button', { name: /reverse/i }))

    const dialog = await screen.findByRole('dialog')
    await click(within(dialog).getByRole('button', { name: /reverse receipt/i }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/needs a reason/i)
    expect(api.reverseReceipt).not.toHaveBeenCalled()

    await type(within(dialog).getByRole('textbox'), 'charged to the wrong patient')
    await click(within(dialog).getByRole('button', { name: /reverse receipt/i }))
    await waitFor(() => expect(api.reverseReceipt).toHaveBeenCalledWith(
      1, 'charged to the wrong patient',
    ))
  })

  it('keeps a reversed receipt visible rather than removing it', async () => {
    api.listReceipts.mockResolvedValue([
      receipt({ status: 'reversed', is_reversed: true }),
      receipt({ id: 2, direction: 'out', receipt_number: 'RCPT-2026-27-000002' }),
    ])
    await openTab(/receipts/i)
    expect(await screen.findByText('RCPT-2026-27-000001')).toBeInTheDocument()
    expect(screen.getByText('RCPT-2026-27-000002')).toBeInTheDocument()
    expect(screen.getByText('Reversed')).toBeInTheDocument()
    // A reversed receipt carries no Reverse button — it cannot be reversed twice.
    expect(screen.queryByRole('button', { name: /^reverse$/i })).toBeNull()
  })

  it('says a reversal needs a second person, and shows the refusal in the dialog', async () => {
    api.listReceipts.mockResolvedValue([receipt()])
    api.reverseReceipt.mockRejectedValue(new Error(
      'a receipt cannot be reversed by the person who took the payment. '
        + 'A second person must sign for a reversal',
    ))
    await openTab(/receipts/i)
    await click(await screen.findByRole('button', { name: /reverse/i }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/second person signs for a reversal/i)
    await type(within(dialog).getByRole('textbox'), 'keyed twice')
    await click(within(dialog).getByRole('button', { name: /reverse receipt/i }))
    expect(await within(dialog).findByRole('alert'))
      .toHaveTextContent(/person who took the payment/i)
  })
})

/* ══ Taking money ════════════════════════════════════════════════════════ */

const openInvoice = (over: Partial<OpenInvoice> = {}): OpenInvoice => ({
  invoice_id: 7, invoice_number: 'INV-2026-27-000007', invoice_type: 'final',
  status: 'issued', issued_at: '2026-08-12 04:00:00',
  payable_paise: 300000, payable_display: '₹3,000.00',
  due_paise: 300000, due_display: '₹3,000.00',
  ...over,
})

describe('choosing the patient', () => {
  it('finds the patient by name and shows the MRN and bed before anything is taken', async () => {
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })
    await choosePatient()

    expect(api.searchCounterPatients).toHaveBeenCalledWith('Ravi')
    const chosen = screen.getByTestId('counter-patient')
    expect(chosen).toHaveTextContent('Ravi Sharma')
    expect(chosen).toHaveTextContent('MRN-GM001')
    expect(chosen).toHaveTextContent('bed GM-101')
    // No free-text ID box is left to type into.
    expect(screen.queryByLabelText(/patient id/i)).toBeNull()
  })

  it('takes nothing for typed text that was never matched to a patient', async () => {
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })
    await type(screen.getByRole('combobox', { name: /patient/i }), 'P001')
    await type(screen.getByLabelText(/amount/i), '100')
    await click(screen.getByRole('button', { name: /take payment/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/which patient/i)
    expect(api.recordPayment).not.toHaveBeenCalled()
  })

  it('says so when no registered patient matches', async () => {
    api.searchCounterPatients.mockResolvedValue([])
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })
    await type(screen.getByRole('combobox', { name: /patient/i }), 'Nobody Here')
    expect(await screen.findByText(/no registered patient matches/i)).toBeInTheDocument()
  })
})

describe('taking a payment against an invoice', () => {
  it("lists only the chosen patient's open bills, with number and balance", async () => {
    api.listOpenInvoices.mockResolvedValue([
      openInvoice(),
      openInvoice({ invoice_id: 9, invoice_number: 'INV-2026-27-000009',
        due_paise: 60000, due_display: '₹600.00', status: 'partially_paid' }),
    ])
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })
    await choosePatient()

    expect(api.listOpenInvoices).toHaveBeenCalledWith('P001')
    const bill = screen.getByLabelText(/bill/i)
    const options = within(bill).getAllByRole('option').map(o => o.textContent)
    expect(options[0]).toMatch(/none, hold as an advance/i)
    expect(options[1]).toContain('INV-2026-27-000007')
    expect(options[1]).toContain('₹3,000.00 due')
    expect(options[2]).toContain('INV-2026-27-000009')
    expect(options[2]).toContain('₹600.00 due')
  })

  it('sends the chosen bill and shows whose it is and what it owes', async () => {
    api.listOpenInvoices.mockResolvedValue([openInvoice()])
    api.recordPayment.mockResolvedValue(receipt({ invoice_id: 7 }))
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })
    await choosePatient()
    await screen.findByRole('option', { name: /INV-2026-27-000007/ })
    await select(screen.getByLabelText(/bill/i), '7')

    const line = screen.getByTestId('invoice-choice')
    expect(line).toHaveTextContent('Ravi Sharma')
    expect(line).toHaveTextContent('₹3,000.00 due')

    await type(screen.getByLabelText(/amount/i), '3000')
    await click(screen.getByRole('button', { name: /take payment/i }))
    await waitFor(() => expect(api.recordPayment).toHaveBeenCalled())
    const sent = api.recordPayment.mock.calls[0][0]
    expect(sent.patientId).toBe('P001')
    expect(sent.invoiceId).toBe(7)
    expect(sent.purpose).toBe('invoice_payment')
  })

  it('holds the money as an advance when the patient has no open bills', async () => {
    api.listOpenInvoices.mockResolvedValue([])
    api.recordPayment.mockResolvedValue(receipt())
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })
    await choosePatient()

    expect(await screen.findByText(/has no open bills/i)).toBeInTheDocument()
    await type(screen.getByLabelText(/amount/i), '500')
    await click(screen.getByRole('button', { name: /take payment/i }))
    await waitFor(() => expect(api.recordPayment).toHaveBeenCalled())
    expect(api.recordPayment.mock.calls[0][0]).toMatchObject({ invoiceId: null, purpose: 'deposit' })
  })

  it('changing the patient clears the bill chosen for the previous one', async () => {
    const asha: CounterPatient = { ...ravi, id: 'P002', name: 'Asha Rao', mrn: 'MRN-GM002' }
    api.listOpenInvoices.mockImplementation(async (pid: string) =>
      pid === 'P001' ? [openInvoice()] : [])
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })
    await choosePatient()
    await screen.findByRole('option', { name: /INV-2026-27-000007/ })
    await select(screen.getByLabelText(/bill/i), '7')

    await click(screen.getByRole('button', { name: /change patient/i }))
    await choosePatient(asha)
    expect(await screen.findByText(/asha rao has no open bills/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/bill/i)).toHaveValue('')
    expect(screen.queryByRole('option', { name: /INV-2026-27-000007/ })).toBeNull()
  })
})

describe('taking a payment', () => {
  it('refuses a traceable method with no reference before the round trip', async () => {
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })

    await choosePatient()
    await type(screen.getByLabelText(/amount/i), '1500')
    await select(screen.getByLabelText(/method/i), 'upi')
    await click(screen.getByRole('button', { name: /take payment/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/reference number/i)
    expect(api.recordPayment).not.toHaveBeenCalled()
  })

  it('converts typed rupees to integer paise without parseFloat', async () => {
    api.recordPayment.mockResolvedValue(receipt({ amount_paise: 150050, amount_display: '₹1,500.50' }))
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })

    await choosePatient()
    await type(screen.getByLabelText(/amount/i), '1500.50')
    await click(screen.getByRole('button', { name: /take payment/i }))

    await waitFor(() => expect(api.recordPayment).toHaveBeenCalled())
    const sent = api.recordPayment.mock.calls[0][0]
    expect(sent.amountPaise).toBe(150050)
    expect(Number.isInteger(sent.amountPaise)).toBe(true)
  })

  it('refuses an amount finer than a paise instead of silently rounding it', async () => {
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })

    await choosePatient()
    await type(screen.getByLabelText(/amount/i), '10.005')
    await click(screen.getByRole('button', { name: /take payment/i }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(api.recordPayment).not.toHaveBeenCalled()
  })

  it('describes an over-payment as an advance the hospital is holding', async () => {
    api.recordPayment.mockResolvedValue(receipt({
      allocated_paise: 300000, allocated_display: '₹3,000.00',
      advance_paise: 200000, advance_display: '₹2,000.00',
      over_payment: true,
      deposit_balance_paise: 200000, deposit_balance_display: '₹2,000.00',
    }))
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })

    api.listOpenInvoices.mockResolvedValue([openInvoice()])
    await choosePatient()
    await type(screen.getByLabelText(/amount/i), '5000')
    await screen.findByRole('option', { name: /INV-2026-27-000007/ })
    await select(screen.getByLabelText(/bill/i), '7')
    await click(screen.getByRole('button', { name: /take payment/i }))

    const slip = await screen.findByText(/held as an advance/i)
    expect(slip.textContent).toContain('₹2,000.00')
    expect(slip.textContent).toContain('₹3,000.00')
    expect(slip.textContent).toMatch(/not a credit on the bill/i)
  })

  it('sends one idempotency key so a double tap cannot take the money twice', async () => {
    api.recordPayment.mockResolvedValue(receipt())
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })

    await choosePatient()
    await type(screen.getByLabelText(/amount/i), '100')
    await click(screen.getByRole('button', { name: /take payment/i }))

    await waitFor(() => expect(api.recordPayment).toHaveBeenCalled())
    expect(api.recordPayment.mock.calls[0][0].idempotencyKey).toBeTruthy()
  })

  it('says a receipt was a replay rather than pretending it is new money', async () => {
    api.recordPayment.mockResolvedValue(receipt({ replayed: true }))
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })

    await choosePatient()
    await type(screen.getByLabelText(/amount/i), '100')
    await click(screen.getByRole('button', { name: /take payment/i }))

    expect(await screen.findByText(/had already been taken/i)).toBeInTheDocument()
  })
})

/* ══ The till ════════════════════════════════════════════════════════════ */

describe('the till', () => {
  const openSession = {
    id: 9, counter_code: 'OPD-1', shift_label: 'morning', opened_by: 'ADM1',
    opened_at: '2026-08-12 03:30:00', opening_float_paise: 200000,
    opening_float_display: '₹2,000.00', closed_by: null, closed_at: null,
    declared_cash_paise: null, declared_cash_display: 'Not counted',
    system_cash_paise: null, system_cash_display: 'Not counted',
    variance_paise: null, variance_display: 'Not counted',
    status: 'open' as const, reconciled_by: null, reconciled_at: null, notes: null,
  }

  it('shows expected cash as float plus cash in minus cash out', async () => {
    api.listCounters.mockResolvedValue([openSession])
    api.getCounterExpected.mockResolvedValue({
      session: openSession,
      opening_float_paise: 200000, opening_float_display: '₹2,000.00',
      cash_in_paise: 150000, cash_in_display: '₹1,500.00',
      cash_out_paise: 40000, cash_out_display: '₹400.00',
      expected_cash_paise: 310000, expected_cash_display: '₹3,100.00',
      by_method: [], totals: {
        in_paise: 150000, out_paise: 40000, net_paise: 110000,
        in_count: 1, out_count: 1,
        in_display: '₹1,500.00', out_display: '₹400.00', net_display: '₹1,100.00',
      },
    })
    render(<PaymentsPanel />)
    const expected = await screen.findByText('₹3,100.00')
    expect(expected).toBeInTheDocument()
    expect(200000 + 150000 - 40000).toBe(310000)
  })

  it('reports a variance as recorded rather than absorbed', async () => {
    api.listCounters.mockResolvedValue([openSession])
    api.getCounterExpected.mockResolvedValue({
      session: openSession,
      opening_float_paise: 200000, opening_float_display: '₹2,000.00',
      cash_in_paise: 0, cash_in_display: '₹0.00',
      cash_out_paise: 0, cash_out_display: '₹0.00',
      expected_cash_paise: 200000, expected_cash_display: '₹2,000.00',
      by_method: [], totals: {
        in_paise: 0, out_paise: 0, net_paise: 0, in_count: 0, out_count: 0,
        in_display: '₹0.00', out_display: '₹0.00', net_display: '₹0.00',
      },
    })
    api.closeCounter.mockResolvedValue({
      ...openSession, status: 'disputed' as const, balanced: false,
      declared_cash_paise: 180000, declared_cash_display: '₹1,800.00',
      system_cash_paise: 200000, system_cash_display: '₹2,000.00',
      variance_paise: -20000, variance_display: '-₹200.00',
    })
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /the till/i })

    await type(screen.getByLabelText(/counted cash/i), '1800')
    await click(screen.getByRole('button', { name: /close shift/i }))

    const message = await screen.findByText(/variance of/i)
    expect(message.textContent).toContain('-₹200.00')
    expect(message.textContent).toMatch(/not written off/i)
  })

  /* THE FAILURE: the till card hid who opened the drawer and when, so a drawer
     left open since an earlier day looked like this morning's. */
  it('shows who opened the till and when, and warns when it was opened on an earlier day', async () => {
    api.listCounters.mockResolvedValue([openSession])   // opened 12 Aug 2026
    render(<PaymentsPanel />)
    const opened = (await screen.findByText('Opened')).closest('div')!
    expect(opened).toHaveTextContent('12 Aug 2026')
    expect(opened).toHaveTextContent('by ADM1')
    expect(screen.getByTestId('till-opened-earlier')).toHaveTextContent(/open since 12 Aug 2026/i)
  })

  it('does not warn about a till opened today', async () => {
    const nowUtc = new Date().toISOString().slice(0, 19).replace('T', ' ')
    api.listCounters.mockResolvedValue([{ ...openSession, opened_at: nowUtc }])
    render(<PaymentsPanel />)
    await screen.findByText('Opened')
    expect(screen.queryByTestId('till-opened-earlier')).toBeNull()
  })

  it('finds a till left open beyond the most recent shifts', async () => {
    api.listCounters.mockImplementation(async (params: { status?: string } = {}) =>
      params.status === 'open' ? [openSession] : [])
    render(<PaymentsPanel />)
    expect(await screen.findByText('OPD-1')).toBeInTheDocument()
    expect(api.listCounters).toHaveBeenCalledWith(expect.objectContaining({ status: 'open' }))
  })

  /* THE FAILURE: with two drawers open, the screen took the first open one, so
     one cashier's cash was counted in another cashier's drawer. */
  it('with two counters open, takes no money until the cashier picks theirs', async () => {
    const other = { ...openSession, id: 10, counter_code: 'IPD-1', opened_by: 'CSH2' }
    api.listCounters.mockResolvedValue([openSession, other])
    api.recordPayment.mockResolvedValue(receipt())
    render(<PaymentsPanel />)
    await screen.findByRole('heading', { name: /take a payment/i })
    expect(screen.getByText(/more than one counter is open/i)).toBeInTheDocument()

    await choosePatient()
    await type(screen.getByLabelText(/amount/i), '100')
    expect(screen.getByRole('button', { name: /take payment/i })).toBeDisabled()

    await select(screen.getByLabelText(/your counter/i), '10')
    const take = screen.getByRole('button', { name: /take payment/i })
    expect(take).toBeEnabled()
    await click(take)
    await waitFor(() => expect(api.recordPayment).toHaveBeenCalled())
    expect(api.recordPayment.mock.calls[0][0].counterSessionId).toBe(10)
  })

  it('defaults to the counter this account opened', async () => {
    localStorage.setItem('orb_user', JSON.stringify({ id: 'CSH2', name: 'Cashier Two', role: 'cashier' }))
    try {
      const other = { ...openSession, id: 10, counter_code: 'IPD-1', opened_by: 'CSH2' }
      api.listCounters.mockResolvedValue([openSession, other])
      api.recordPayment.mockResolvedValue(receipt())
      render(<PaymentsPanel />)
      await screen.findByRole('heading', { name: /take a payment/i })
      expect(screen.queryByText(/more than one counter is open/i)).toBeNull()

      await choosePatient()
      await type(screen.getByLabelText(/amount/i), '100')
      await click(screen.getByRole('button', { name: /take payment/i }))
      await waitFor(() => expect(api.recordPayment).toHaveBeenCalled())
      expect(api.recordPayment.mock.calls[0][0].counterSessionId).toBe(10)
    } finally {
      localStorage.removeItem('orb_user')
    }
  })
})

/* ══ Approvals ═══════════════════════════════════════════════════════════ */

describe('approvals', () => {
  it('shows both names on a discount — who asked and who agreed', async () => {
    api.listDiscounts.mockResolvedValue([{
      id: 3, patient_id: 'P001', admission_id: null, invoice_id: null,
      encounter_charge_id: null, scope: 'invoice' as const,
      discount_type: 'amount' as const, percent_bp: 0,
      requested_paise: 250000, requested_display: '₹2,500.00',
      approved_paise: 200000, approved_display: '₹2,000.00',
      policy_limit_paise: null, policy_limit_display: 'No limit configured',
      exceeded_limit: false, reason_code: 'HARDSHIP', reason: 'hardship case',
      status: 'approved' as const, requested_by: 'CLERK1',
      requested_at: '2026-08-12 04:00:00', decided_by: 'MGR1',
      decided_at: '2026-08-12 04:10:00', decision_note: null,
    }])
    await openTab(/approvals/i)
    const row = (await screen.findByText('hardship case')).closest('li')!
    expect(within(row).getByText(/CLERK1/)).toBeInTheDocument()
    expect(row.textContent).toContain('₹2,000.00')
  })

  it('renders an undecided discount as "Not decided", never as ₹0.00', async () => {
    api.listDiscounts.mockResolvedValue([{
      id: 4, patient_id: 'P002', admission_id: null, invoice_id: null,
      encounter_charge_id: null, scope: 'invoice' as const,
      discount_type: 'amount' as const, percent_bp: 0,
      requested_paise: 100000, requested_display: '₹1,000.00',
      approved_paise: null, approved_display: 'Not decided',
      policy_limit_paise: null, policy_limit_display: 'No limit configured',
      exceeded_limit: false, reason_code: null, reason: 'awaiting sign-off',
      status: 'requested' as const, requested_by: 'CLERK1',
      requested_at: '2026-08-12 04:00:00', decided_by: null,
      decided_at: null, decision_note: null,
    }])
    await openTab(/approvals/i)
    const row = (await screen.findByText('awaiting sign-off')).closest('li')!
    expect(within(row).getByText('Not decided')).toBeInTheDocument()
    expect(within(row).queryByText('₹0.00')).toBeNull()
  })

  it('flags a discount that went over the policy limit', async () => {
    api.listDiscounts.mockResolvedValue([{
      id: 5, patient_id: 'P003', admission_id: null, invoice_id: null,
      encounter_charge_id: null, scope: 'invoice' as const,
      discount_type: 'amount' as const, percent_bp: 0,
      requested_paise: 5000000, requested_display: '₹50,000.00',
      approved_paise: 5000000, approved_display: '₹50,000.00',
      policy_limit_paise: 1000000, policy_limit_display: '₹10,000.00',
      exceeded_limit: true, reason_code: null, reason: 'board approval',
      status: 'approved' as const, requested_by: 'CLERK1',
      requested_at: '2026-08-12 04:00:00', decided_by: 'DIR1',
      decided_at: '2026-08-12 04:30:00', decision_note: null,
    }])
    await openTab(/approvals/i)
    expect(await screen.findByText(/over the policy limit/i)).toBeInTheDocument()
  })

  it('names the patient and the person who raised a refund', async () => {
    api.listRefunds.mockResolvedValue([{
      id: 9, refund_number: 'REF-2026-27-000002', patient_id: 'PAT-50A2D09E',
      patient_name: 'Ravi Sharma', patient_mrn: 'MRN-GM001',
      admission_id: null, invoice_id: null, payment_id: 1,
      amount_paise: 50000, amount_display: '₹500.00',
      reason_code: null, reason: 'procedure not done',
      status: 'requested' as const, method: null, reference_no: null,
      requested_by: 'CLERK1', requested_by_name: 'Asha Clerk',
      requested_at: '2026-08-12 05:00:00',
      decided_by: null, decided_at: null, decision_note: null,
      paid_by: null, paid_at: null,
    }])
    await openTab(/approvals/i)
    const row = (await screen.findByText('REF-2026-27-000002')).closest('li')!
    expect(row).toHaveTextContent('Ravi Sharma')
    expect(row).toHaveTextContent('Raised by Asha Clerk')
    expect(row).not.toHaveTextContent('PAT-50A2D09E')
  })

  it('never sends an approver name from the client', async () => {
    api.listRefunds.mockResolvedValue([{
      id: 8, refund_number: 'REF-2026-27-000001', patient_id: 'P001',
      admission_id: null, invoice_id: null, payment_id: 1,
      amount_paise: 50000, amount_display: '₹500.00',
      reason_code: null, reason: 'cancelled procedure',
      status: 'requested' as const, method: null, reference_no: null,
      requested_by: 'CLERK1', requested_at: '2026-08-12 05:00:00',
      decided_by: null, decided_at: null, decision_note: null,
      paid_by: null, paid_at: null,
    }])
    api.decideRefund.mockResolvedValue({})
    await openTab(/approvals/i)
    await click(await screen.findByRole('button', { name: /approve/i }))
    await waitFor(() => expect(api.decideRefund).toHaveBeenCalled())
    // Only the id and the verdict cross the wire; the approver is the session.
    expect(api.decideRefund).toHaveBeenCalledWith(8, true)
  })
})
