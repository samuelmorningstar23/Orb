/**
 * Orb Hospital OS — Payments API
 *
 * Receipts, deposits/advances, refunds, discount approvals and the day-end cash
 * count. Mirrors server/routes/payments_routes.py.
 *
 * THREE RULES THIS CLIENT ENFORCES ON ITS OWN SIDE
 * ────────────────────────────────────────────────
 * 1. EVERY AMOUNT ON THE WIRE IS INTEGER PAISE. `amountPaise` is typed `number`
 *    and is validated with `assertPaise` from lib/money before it is sent. JSON
 *    has one number type and it is a double, so `1234.5` would sail through a
 *    naive client and land in a money column as a rupee figure in the wrong box.
 *    The server refuses it too — this is the second lock, not the only one.
 *
 * 2. A RECEIPT IS NEVER EDITED. There is deliberately no `updateReceipt()` in
 *    this file. The only correction is `reverseReceipt()`, which the server
 *    implements as a new opposing entry. If you find yourself wanting a PATCH,
 *    read the note on `RECEIPT_EDIT_REFUSED` below.
 *
 * 3. `null` IS NOT ZERO. Amounts that may legitimately be unknown — an uncounted
 *    drawer, an undecided discount — arrive as `number | null` and MUST be
 *    rendered through `formatInrOrUnset`, never through `?? 0`.
 */
import { apiFetch } from './client'
import { assertPaise } from '../lib/money'

/* ── Vocabulary (mirrors the CHECK constraints in migration 20) ───────────── */

export type PaymentMethod =
  | 'cash' | 'card' | 'upi' | 'netbanking' | 'cheque' | 'dd' | 'neft' | 'rtgs'
  | 'wallet' | 'insurance' | 'scheme' | 'adjustment'

export type ReceiptPurpose = 'advance' | 'deposit' | 'invoice_payment' | 'settlement'

export type PaymentStatus = 'received' | 'cleared' | 'bounced' | 'cancelled' | 'reversed'

export type RefundStatus = 'requested' | 'approved' | 'rejected' | 'paid' | 'cancelled'

export type DiscountStatus = 'requested' | 'approved' | 'rejected' | 'cancelled'

export type CounterStatus = 'open' | 'closed' | 'reconciled' | 'disputed'

/** Methods the server will refuse without a reference number — a card slip, a
 *  UPI UTR, a cheque number. Kept here so the form can mark the field required
 *  before the round trip rather than after it. */
export const REFERENCE_REQUIRED_METHODS: readonly PaymentMethod[] = [
  'card', 'upi', 'netbanking', 'cheque', 'dd', 'neft', 'rtgs', 'wallet',
]

/** What a clerk can select at a counter. `insurance`/`scheme`/`adjustment` are
 *  produced by the payer and deposit paths, not typed by hand. */
export const COUNTER_METHODS: readonly PaymentMethod[] = [
  'cash', 'upi', 'card', 'netbanking', 'neft', 'rtgs', 'cheque', 'dd',
]

/** The one message a UI should show if it ever finds itself with a PATCH.
 *  There is no function here to send one. */
export const RECEIPT_EDIT_REFUSED =
  'A receipt cannot be edited once issued. Reverse it and record a new one.'

/* ── Shapes ───────────────────────────────────────────────────────────────── */

/** Names the server puts beside the ids on a list a person reads. Absent on
 *  a write response; null when the id names nobody, or names a patient outside
 *  the caller's ward. */
export interface PatientNames {
  patient_name?: string | null
  patient_mrn?: string | null
}

export interface Receipt extends PatientNames {
  id: number
  receipt_number: string | null
  patient_id: string
  admission_id: string | null
  invoice_id: number | null
  payer_id: number | null
  direction: 'in' | 'out'
  amount_paise: number
  amount_display: string
  /** Ledger view: money out is negative. The row itself stores a positive
   *  amount plus a direction — the sign lives in one place, not two. */
  signed_paise: number
  method: PaymentMethod
  reference_no: string | null
  bank_name: string | null
  instrument_date: string | null
  purpose: string
  status: PaymentStatus
  counter_session_id: number | null
  refund_id: number | null
  reversal_of_payment_id: number | null
  received_by: string | null
  received_by_name?: string | null
  received_at: string | null
  cancelled_by: string | null
  cancelled_by_name?: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  notes: string | null
  is_reversed: boolean
  is_immutable: boolean
  /** Present on the create response. */
  replayed?: boolean
  allocated_paise?: number
  allocated_display?: string
  advance_paise?: number
  advance_display?: string
  deposit_balance_paise?: number
  deposit_balance_display?: string
  over_payment?: boolean
  receipt_series_is_configured?: boolean
  allocations?: {
    id: number
    invoice_id: number
    amount_paise: number
    amount_display: string
    reversed_at: string | null
  }[]
}

export interface ReversalResult {
  original: Receipt
  reversal: Receipt
  invoices_recomputed: number[]
  advance_withdrawn_paise: number
}

export interface DepositEntry {
  id: number
  movement: 'deposit' | 'apply' | 'refund' | 'forfeit' | 'transfer'
  /** SIGNED: positive adds to the advance, negative takes from it. */
  amount_paise: number
  amount_display: string
  payment_id: number | null
  invoice_id: number | null
  created_by: string | null
  created_at: string | null
  notes: string | null
}

export interface DepositAccount {
  patient_id: string
  admission_id: string | null
  balance_paise: number
  balance_display: string
  entry_count: number
  entries: DepositEntry[]
}

export interface Refund extends PatientNames {
  id: number
  refund_number: string | null
  patient_id: string
  admission_id: string | null
  invoice_id: number | null
  payment_id: number | null
  amount_paise: number
  amount_display: string
  reason_code: string | null
  reason: string
  status: RefundStatus
  method: PaymentMethod | null
  reference_no: string | null
  requested_by: string | null
  requested_by_name?: string | null
  requested_at: string | null
  decided_by: string | null
  decided_by_name?: string | null
  decided_at: string | null
  decision_note: string | null
  paid_by: string | null
  paid_by_name?: string | null
  paid_at: string | null
  payment?: Receipt
}

export interface RefundLimits {
  patient_id: string
  scope: 'payment' | 'invoice' | 'patient'
  paid_paise: number
  already_refunded_paise: number
  refundable_paise: number
  refundable_display: string
  deposit_balance_paise: number
  deposit_balance_display: string
}

export interface DiscountApproval extends PatientNames {
  id: number
  patient_id: string
  admission_id: string | null
  invoice_id: number | null
  encounter_charge_id: number | null
  scope: 'invoice' | 'charge' | 'patient'
  discount_type: 'amount' | 'percent'
  percent_bp: number
  requested_paise: number
  requested_display: string
  /** null until somebody decides. NOT zero — render with formatInrOrUnset. */
  approved_paise: number | null
  approved_display: string
  policy_limit_paise: number | null
  policy_limit_display: string
  exceeded_limit: boolean
  reason_code: string | null
  reason: string
  status: DiscountStatus
  requested_by: string | null
  requested_by_name?: string | null
  requested_at: string | null
  decided_by: string | null
  decided_by_name?: string | null
  decided_at: string | null
  decision_note: string | null
}

export interface CounterSession {
  id: number
  counter_code: string
  shift_label: string | null
  opened_by: string | null
  opened_by_name?: string | null
  opened_at: string | null
  opening_float_paise: number
  opening_float_display: string
  closed_by: string | null
  closed_at: string | null
  /** null while the drawer has not been counted. "Not counted", never ₹0.00. */
  declared_cash_paise: number | null
  declared_cash_display: string
  system_cash_paise: number | null
  system_cash_display: string
  variance_paise: number | null
  variance_display: string
  status: CounterStatus
  reconciled_by: string | null
  reconciled_at: string | null
  notes: string | null
  /** Present on the close response. */
  expected_cash_paise?: number
  expected_cash_display?: string
  balanced?: boolean
  by_method?: MethodRow[]
  totals?: Totals
}

export interface MethodRow {
  method: PaymentMethod
  in_paise: number
  out_paise: number
  net_paise: number
  in_count: number
  out_count: number
  in_display: string
  out_display: string
  net_display: string
}

export interface Totals {
  in_paise: number
  out_paise: number
  net_paise: number
  in_count: number
  out_count: number
  in_display: string
  out_display: string
  net_display: string
}

export interface CounterExpected {
  session: CounterSession
  opening_float_paise: number
  opening_float_display: string
  cash_in_paise: number
  cash_in_display: string
  cash_out_paise: number
  cash_out_display: string
  expected_cash_paise: number
  expected_cash_display: string
  by_method: MethodRow[]
  totals: Totals
}

export interface DayEnd {
  day: string
  window_utc: { from: string; to: string }
  counter_code: string | null
  by_method: MethodRow[]
  totals: Totals
  cash_in_paise: number
  cash_out_paise: number
  cash_net_paise: number
  cash_net_display: string
  sessions: CounterSession[]
  /** null while any session is still open — the day is not counted yet. */
  counted_cash_paise: number | null
  counted_cash_display: string
  variance_paise: number | null
  variance_display: string
  open_session_count: number
  /** Why the sheet is not final, in words: an open drawer (named when it was
   *  opened on an earlier day) or cash taken with no counter. */
  finality_blockers?: string[]
  is_final: boolean
  open_refund_count: number
  open_discount_count: number
  receipt_series_is_configured: boolean
}

export interface PatientMoneySummary {
  patient_id: string
  admission_id: string | null
  receipt_count: number
  paid_paise: number
  paid_display: string
  refunded_paise: number
  refunded_display: string
  net_paise: number
  net_display: string
  deposit_balance_paise: number
  deposit_balance_display: string
  open_refund_count: number
  open_discount_count: number
  /** Always true. Nothing in this module may gate a clinical action. */
  advisory_only: boolean
}

export interface PaymentsConfig {
  module: 'payments'
  enabled: boolean
  methods: PaymentMethod[]
  reference_required_methods: PaymentMethod[]
  receipt_purposes: ReceiptPurpose[]
  refund_methods: PaymentMethod[]
  receipts_are_immutable: boolean
  receipt_series: {
    prefix: string | null
    financial_year: string | null
    next_number: number | null
    is_configured: boolean
  } | null
}

/* ── Guards ───────────────────────────────────────────────────────────────── */

/** Refuse to put a non-integer into a money field before it leaves the browser.
 *  `assertPaise` rejects floats, bools and unsafe integers by name, so the error
 *  a clerk sees names the field rather than arriving as a 422 from the server. */
function paise(value: number, name: string): number {
  return assertPaise(value, name)
}

/* ── Receipts ─────────────────────────────────────────────────────────────── */

export interface RecordPaymentInput {
  patientId: string
  amountPaise: number
  method: PaymentMethod
  purpose?: ReceiptPurpose
  invoiceId?: number | null
  admissionId?: string | null
  payerId?: number | null
  referenceNo?: string | null
  bankName?: string | null
  instrumentDate?: string | null
  counterSessionId?: number | null
  notes?: string | null
  /** Send one for anything a clerk can double-tap. The server is idempotent on
   *  it, so a replay returns the SAME receipt instead of taking the money twice. */
  idempotencyKey?: string
}

export async function recordPayment(input: RecordPaymentInput): Promise<Receipt> {
  return apiFetch<Receipt>('/payments/receipts', {
    method: 'POST',
    body: JSON.stringify({
      patient_id: input.patientId,
      amount_paise: paise(input.amountPaise, 'amount_paise'),
      method: input.method,
      purpose: input.purpose ?? 'invoice_payment',
      invoice_id: input.invoiceId ?? null,
      admission_id: input.admissionId ?? null,
      payer_id: input.payerId ?? null,
      reference_no: input.referenceNo ?? null,
      bank_name: input.bankName ?? null,
      instrument_date: input.instrumentDate ?? null,
      counter_session_id: input.counterSessionId ?? null,
      notes: input.notes ?? null,
      idempotency_key: input.idempotencyKey ?? null,
    }),
  })
}

export async function listReceipts(params: {
  patientId?: string
  invoiceId?: number
  counterSessionId?: number
  status?: PaymentStatus
  direction?: 'in' | 'out'
  limit?: number
} = {}): Promise<Receipt[]> {
  const q = new URLSearchParams()
  if (params.patientId) q.set('patient_id', params.patientId)
  if (params.invoiceId != null) q.set('invoice_id', String(params.invoiceId))
  if (params.counterSessionId != null) q.set('counter_session_id', String(params.counterSessionId))
  if (params.status) q.set('status', params.status)
  if (params.direction) q.set('direction', params.direction)
  if (params.limit != null) q.set('limit', String(params.limit))
  const qs = q.toString()
  const res = await apiFetch<{ receipts: Receipt[] }>(`/payments/receipts${qs ? `?${qs}` : ''}`)
  return res.receipts
}

export async function getReceipt(id: number): Promise<Receipt> {
  return apiFetch<Receipt>(`/payments/receipts/${id}`)
}

/** The ONLY correction path. There is no updateReceipt(). */
export async function reverseReceipt(id: number, reason: string): Promise<ReversalResult> {
  return apiFetch<ReversalResult>(`/payments/receipts/${id}/reverse`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

/* ── Deposits and advances ────────────────────────────────────────────────── */

export async function getDeposits(
  patientId: string,
  opts: { admissionId?: string; limit?: number } = {},
): Promise<DepositAccount> {
  const q = new URLSearchParams()
  if (opts.admissionId) q.set('admission_id', opts.admissionId)
  if (opts.limit != null) q.set('limit', String(opts.limit))
  const qs = q.toString()
  return apiFetch<DepositAccount>(
    `/payments/deposits/${encodeURIComponent(patientId)}${qs ? `?${qs}` : ''}`,
  )
}

export async function applyDeposit(input: {
  patientId: string
  invoiceId: number
  amountPaise: number
  admissionId?: string | null
  notes?: string | null
}): Promise<Receipt> {
  return apiFetch<Receipt>('/payments/deposits/apply', {
    method: 'POST',
    body: JSON.stringify({
      patient_id: input.patientId,
      invoice_id: input.invoiceId,
      amount_paise: paise(input.amountPaise, 'amount_paise'),
      admission_id: input.admissionId ?? null,
      notes: input.notes ?? null,
    }),
  })
}

/* ── Refunds ──────────────────────────────────────────────────────────────── */

export async function getRefundLimits(
  patientId: string,
  opts: { paymentId?: number; invoiceId?: number } = {},
): Promise<RefundLimits> {
  const q = new URLSearchParams({ patient_id: patientId })
  if (opts.paymentId != null) q.set('payment_id', String(opts.paymentId))
  if (opts.invoiceId != null) q.set('invoice_id', String(opts.invoiceId))
  return apiFetch<RefundLimits>(`/payments/refunds/limits?${q.toString()}`)
}

export async function requestRefund(input: {
  patientId: string
  amountPaise: number
  reason: string
  paymentId?: number | null
  invoiceId?: number | null
  admissionId?: string | null
  reasonCode?: string | null
  idempotencyKey?: string
}): Promise<Refund> {
  return apiFetch<Refund>('/payments/refunds', {
    method: 'POST',
    body: JSON.stringify({
      patient_id: input.patientId,
      amount_paise: paise(input.amountPaise, 'amount_paise'),
      reason: input.reason,
      payment_id: input.paymentId ?? null,
      invoice_id: input.invoiceId ?? null,
      admission_id: input.admissionId ?? null,
      reason_code: input.reasonCode ?? null,
      idempotency_key: input.idempotencyKey ?? null,
    }),
  })
}

export async function listRefunds(params: {
  patientId?: string
  status?: RefundStatus
  limit?: number
} = {}): Promise<Refund[]> {
  const q = new URLSearchParams()
  if (params.patientId) q.set('patient_id', params.patientId)
  if (params.status) q.set('status', params.status)
  if (params.limit != null) q.set('limit', String(params.limit))
  const qs = q.toString()
  const res = await apiFetch<{ refunds: Refund[] }>(`/payments/refunds${qs ? `?${qs}` : ''}`)
  return res.refunds
}

/** The approver's identity comes from the SESSION on the server. There is no
 *  `decidedBy` field here on purpose: a body-supplied approver is a signature
 *  anybody can forge. */
export async function decideRefund(
  id: number,
  approve: boolean,
  decisionNote?: string,
): Promise<Refund> {
  return apiFetch<Refund>(`/payments/refunds/${id}/decision`, {
    method: 'POST',
    body: JSON.stringify({ approve, decision_note: decisionNote ?? null }),
  })
}

export async function payRefund(input: {
  refundId: number
  method: PaymentMethod
  referenceNo?: string | null
  counterSessionId?: number | null
}): Promise<Refund> {
  return apiFetch<Refund>(`/payments/refunds/${input.refundId}/pay`, {
    method: 'POST',
    body: JSON.stringify({
      method: input.method,
      reference_no: input.referenceNo ?? null,
      counter_session_id: input.counterSessionId ?? null,
    }),
  })
}

/* ── Discount approvals ───────────────────────────────────────────────────── */

export async function requestDiscount(input: {
  patientId: string
  reason: string
  requestedPaise?: number
  percentBp?: number
  basePaise?: number
  discountType?: 'amount' | 'percent'
  scope?: 'invoice' | 'charge' | 'patient'
  invoiceId?: number | null
  encounterChargeId?: number | null
  policyLimitPaise?: number | null
  reasonCode?: string | null
  admissionId?: string | null
}): Promise<DiscountApproval> {
  return apiFetch<DiscountApproval>('/payments/discounts', {
    method: 'POST',
    body: JSON.stringify({
      patient_id: input.patientId,
      reason: input.reason,
      requested_paise:
        input.requestedPaise == null ? null : paise(input.requestedPaise, 'requested_paise'),
      percent_bp: input.percentBp ?? 0,
      base_paise: input.basePaise == null ? null : paise(input.basePaise, 'base_paise'),
      discount_type: input.discountType ?? 'amount',
      scope: input.scope ?? 'invoice',
      invoice_id: input.invoiceId ?? null,
      encounter_charge_id: input.encounterChargeId ?? null,
      policy_limit_paise:
        input.policyLimitPaise == null ? null : paise(input.policyLimitPaise, 'policy_limit_paise'),
      reason_code: input.reasonCode ?? null,
      admission_id: input.admissionId ?? null,
    }),
  })
}

export async function listDiscounts(params: {
  status?: DiscountStatus
  invoiceId?: number
  patientId?: string
  limit?: number
} = {}): Promise<DiscountApproval[]> {
  const q = new URLSearchParams()
  if (params.status) q.set('status', params.status)
  if (params.invoiceId != null) q.set('invoice_id', String(params.invoiceId))
  if (params.patientId) q.set('patient_id', params.patientId)
  if (params.limit != null) q.set('limit', String(params.limit))
  const qs = q.toString()
  const res = await apiFetch<{ discounts: DiscountApproval[] }>(
    `/payments/discounts${qs ? `?${qs}` : ''}`,
  )
  return res.discounts
}

/** Approve or reject. The approver is the signed-in user, taken from the
 *  session server-side — never sent from here. */
export async function decideDiscount(
  id: number,
  approve: boolean,
  opts: { approvedPaise?: number; decisionNote?: string } = {},
): Promise<DiscountApproval> {
  return apiFetch<DiscountApproval>(`/payments/discounts/${id}/decision`, {
    method: 'POST',
    body: JSON.stringify({
      approve,
      approved_paise:
        opts.approvedPaise == null ? null : paise(opts.approvedPaise, 'approved_paise'),
      decision_note: opts.decisionNote ?? null,
    }),
  })
}

/* ── Counters and day-end ─────────────────────────────────────────────────── */

export async function openCounter(input: {
  counterCode: string
  shiftLabel?: string | null
  openingFloatPaise?: number
}): Promise<CounterSession> {
  return apiFetch<CounterSession>('/payments/counters/open', {
    method: 'POST',
    body: JSON.stringify({
      counter_code: input.counterCode,
      shift_label: input.shiftLabel ?? null,
      opening_float_paise: paise(input.openingFloatPaise ?? 0, 'opening_float_paise'),
    }),
  })
}

export async function listCounters(params: {
  counterCode?: string
  status?: CounterStatus
  limit?: number
} = {}): Promise<CounterSession[]> {
  const q = new URLSearchParams()
  if (params.counterCode) q.set('counter_code', params.counterCode)
  if (params.status) q.set('status', params.status)
  if (params.limit != null) q.set('limit', String(params.limit))
  const qs = q.toString()
  const res = await apiFetch<{ sessions: CounterSession[] }>(
    `/payments/counters${qs ? `?${qs}` : ''}`,
  )
  return res.sessions
}

export async function getCounterExpected(sessionId: number): Promise<CounterExpected> {
  return apiFetch<CounterExpected>(`/payments/counters/${sessionId}/expected`)
}

export async function closeCounter(
  sessionId: number,
  declaredCashPaise: number,
  notes?: string,
): Promise<CounterSession> {
  return apiFetch<CounterSession>(`/payments/counters/${sessionId}/close`, {
    method: 'POST',
    body: JSON.stringify({
      declared_cash_paise: paise(declaredCashPaise, 'declared_cash_paise'),
      notes: notes ?? null,
    }),
  })
}

export async function reconcileCounter(
  sessionId: number,
  notes?: string,
): Promise<CounterSession> {
  return apiFetch<CounterSession>(`/payments/counters/${sessionId}/reconcile`, {
    method: 'POST',
    body: JSON.stringify({ notes: notes ?? null }),
  })
}

export async function getDayEnd(params: { day?: string; counterCode?: string } = {}): Promise<DayEnd> {
  const q = new URLSearchParams()
  if (params.day) q.set('day', params.day)
  if (params.counterCode) q.set('counter_code', params.counterCode)
  const qs = q.toString()
  return apiFetch<DayEnd>(`/payments/day-end${qs ? `?${qs}` : ''}`)
}

export async function getPaymentsConfig(): Promise<PaymentsConfig> {
  return apiFetch<PaymentsConfig>('/payments/config')
}

export async function getPatientMoneySummary(
  patientId: string,
  admissionId?: string,
): Promise<PatientMoneySummary> {
  const q = admissionId ? `?admission_id=${encodeURIComponent(admissionId)}` : ''
  return apiFetch<PatientMoneySummary>(
    `/payments/patients/${encodeURIComponent(patientId)}/summary${q}`,
  )
}

/** The invoice a payment is about to be applied to, as the server sees it.
 *  Another patient's invoice is refused with a 422 that names no one. */
export interface PaymentInvoiceCheck {
  invoice_id: number
  invoice_number: string | null
  patient_id: string
  patient_name: string | null
  status: string
  allocatable: boolean
  payable_paise: number
  payable_display: string
  due_paise: number
  due_display: string
}

export async function checkPaymentInvoice(
  patientId: string,
  invoiceId: number,
): Promise<PaymentInvoiceCheck> {
  return apiFetch<PaymentInvoiceCheck>(
    `/payments/patients/${encodeURIComponent(patientId)}/invoices/${invoiceId}`,
  )
}

/** A patient the counter can take money for: who and where, nothing clinical. */
export interface CounterPatient {
  id: string
  name: string
  mrn: string
  bed: string | null
  ward: string | null
  status: 'admitted' | 'discharged' | 'transferred' | string
}

/** Find the patient at the counter by name, MRN, bed or ID. Under two
 *  characters the server answers with nobody, so this does not ask. */
export async function searchCounterPatients(query: string): Promise<CounterPatient[]> {
  const q = query.trim()
  if (q.length < 2) return []
  const res = await apiFetch<{ patients: CounterPatient[] }>(
    `/payments/patients?q=${encodeURIComponent(q)}`,
  )
  return res.patients
}

/** One of a patient's bills that still owes money. */
export interface OpenInvoice {
  invoice_id: number
  invoice_number: string | null
  invoice_type: string
  status: string
  issued_at: string | null
  payable_paise: number
  payable_display: string
  due_paise: number
  due_display: string
}

/** This patient's bills that still owe money, oldest first, for the counter
 *  to offer as a choice instead of an invoice number typed by hand. */
export async function listOpenInvoices(patientId: string): Promise<OpenInvoice[]> {
  const res = await apiFetch<{ invoices: OpenInvoice[] }>(
    `/payments/patients/${encodeURIComponent(patientId)}/invoices`,
  )
  return res.invoices
}

/* ── Local helpers a screen needs ─────────────────────────────────────────── */

/** An MRN as a clerk reads it. A site whose MRNs already carry the prefix
 *  ("MRN-GM001") prints them as they are; a bare number gets the label. */
export function mrnText(mrn: string): string {
  return /^mrn/i.test(mrn.trim()) ? mrn : `MRN ${mrn}`
}

/** True when this method cannot be recorded without a reference number. */
export function needsReference(method: PaymentMethod): boolean {
  return REFERENCE_REQUIRED_METHODS.includes(method)
}

/** A stable key for one counter action, so a double-tap replays instead of
 *  charging twice. Regenerate it only when the FORM is reset. */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}
