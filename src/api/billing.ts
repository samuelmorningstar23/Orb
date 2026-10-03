/**
 * Orb — billing API client.
 *
 * Thin, typed wrappers over /api/billing. Two rules this file exists to hold:
 *
 * 1. MONEY CROSSES THE WIRE AS INTEGER PAISE, ALWAYS. Every amount in these
 *    types is a `*Paise` number and is rendered through `src/lib/money.ts` — the
 *    exact mirror of the server's `services/money.py`. Nothing here parses a
 *    formatted string back into a number, and nothing multiplies a rupee float.
 *
 * 2. "NOT CONFIGURED" SURVIVES THE TRIP. A rate nobody has set arrives as
 *    `rate_paise: null` with `rate_is_configured: false`, and the UI must render
 *    `formatInrOrUnset(...)`. A client that coalesces the null to 0 puts a price
 *    of ₹0.00 on a screen, and somebody bills it.
 *
 * The server also sends pre-formatted `*_display` strings for every amount. They
 * are the SERVER's rendering of the same paise, kept so a printed document and
 * an exported CSV read identically to the screen; the panel prefers them for
 * totals it did not compute itself, and uses lib/money.ts for anything it
 * derives locally. The two agree because the two modules are a pair.
 */
import { apiFetch } from './client'

/* ── Types ─────────────────────────────────────────────────────────────────── */

export type GstTreatment = 'exempt' | 'taxable' | 'nil_rated' | 'zero_rated'

export type InvoiceStatus =
  | 'draft' | 'issued' | 'partially_paid' | 'paid' | 'cancelled' | 'void' | 'written_off'

export type InvoiceType = 'interim' | 'final' | 'advance' | 'credit_note' | 'debit_note'

export interface ServiceItem {
  id: number
  code: string
  name: string
  category: string
  department: string | null
  uom: string
  gst_treatment: GstTreatment
  gst_rate_bp: number
  hsn_sac: string | null
  default_rate_paise: number
  /** 0 means NO PRICE SET. It does not mean ₹0. */
  rate_is_configured: number
  is_active: number
  notes: string | null
}

export interface TariffPlan {
  id: number
  code: string
  name: string
  plan_type: string
  is_default: number
  is_active: number
}

export interface TariffRate {
  id: number
  tariff_plan_id: number
  service_code: string
  service_name: string | null
  ward_class: string | null
  rate_paise: number
  rate_display: string
  effective_from: string
  effective_to: string | null
}

export interface PackageRate {
  id: number
  tariff_plan_id: number
  package_code: string
  name: string
  ward_class: string | null
  package_rate_paise: number
  package_rate_display: string
  los_days_included: number | null
  external_scheme_code: string | null
}

export interface RateResolution {
  service_code: string
  /** null when nobody has priced this item — render the words, never a zero. */
  rate_paise: number | null
  rate_display: string
  rate_is_configured: boolean
  source: 'tariff_rate' | 'service_default' | 'unconfigured' | 'unknown_item'
  tariff_plan_code: string | null
  gst_treatment: GstTreatment
  gst_rate_bp: number
  description: string | null
  hsn_sac: string | null
  uom: string | null
}

export interface Charge {
  id: number
  patient_id: string
  admission_id: string | null
  encounter_type: string
  service_code: string
  description: string
  quantity_milli: number
  quantity: number
  unit_rate_paise: number
  unit_rate_display: string
  rate_is_configured: number
  gross_paise: number
  gross_display: string
  discount_paise: number
  gst_treatment: GstTreatment
  gst_rate_bp: number
  taxable_value_paise: number
  cgst_paise: number
  sgst_paise: number
  igst_paise: number
  tax_paise: number
  tax_display: string
  total_paise: number
  total_display: string
  status: 'pending' | 'billed' | 'cancelled' | 'written_off' | 'packaged'
  invoice_id: number | null
  source: string
  source_ref: string | null
  service_date: string | null
  cancel_reason: string | null
  replayed?: boolean
}

export interface InvoiceLine {
  id: number
  line_no: number
  encounter_charge_id: number | null
  service_code: string
  description: string
  hsn_sac: string | null
  quantity: number
  uom: string | null
  unit_rate_paise: number
  unit_rate_display: string
  gross_paise: number
  gross_display: string
  discount_paise: number
  gst_treatment: GstTreatment
  gst_rate_bp: number
  taxable_value_paise: number
  cgst_paise: number
  sgst_paise: number
  igst_paise: number
  tax_paise: number
  tax_display: string
  total_paise: number
  total_display: string
}

export interface Invoice {
  id: number
  invoice_number: string | null
  number_display: string
  patient_id: string
  patient_name?: string | null
  patient_mrn?: string | null
  admission_id: string | null
  encounter_type: string
  invoice_type: InvoiceType
  status: InvoiceStatus
  is_interstate: number
  is_credit_note: boolean
  subtotal_paise: number
  discount_paise: number
  taxable_value_paise: number
  cgst_paise: number
  sgst_paise: number
  igst_paise: number
  tax_paise: number
  total_paise: number
  rounding_adjustment_paise: number
  rounding_adjustment_display: string
  payable_paise: number
  payable_display: string
  amount_paid_paise: number
  amount_paid_display: string
  balance_paise: number
  balance_display: string
  supersedes_invoice_id: number | null
  issued_at: string | null
  /** The service period the bill covers, when the invoice recorded one. */
  period_from?: string | null
  period_to?: string | null
  /** The hospital's configured name, or null when none is set. */
  supplier_name?: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  notes: string | null
  lines?: InvoiceLine[]
  patient?: { id: string; name: string; mrn: string; age: number | null; sex: string | null } | null
  payer?: { id: number; code: string; name: string; payer_type: string } | null
  payments?: {
    id: number; receipt_number: string | null; amount_display: string; method: string; status: string
    direction?: 'in' | 'out'; received_at?: string | null
  }[]
  credit_notes?: { id: number; invoice_number: string | null; payable_display: string }[]
  allowed_transitions?: string[]
}

export interface CaptureCandidate {
  source: string
  source_ref: string
  description: string
  kind: string
  service_date: string | null
  service_code?: string
  total_paise?: number
  total_display?: string
  rate_display?: string
  charge_id?: number
  would_post?: boolean
  reason?: 'already_charged' | 'no_service_code' | 'no_service_item' | 'rate_not_configured'
  hint?: string
}

export interface CaptureResult {
  patient_id: string
  dry_run: boolean
  posted: CaptureCandidate[]
  skipped: CaptureCandidate[]
  posted_count: number
  posted_total_paise: number
  posted_total_display: string
  /** Clinical events this bill CANNOT include. The honest headline. */
  unpriceable_count: number
}

export interface FinancialSummary {
  patient_id: string
  admission_id: string | null
  live_invoice_count: number
  draft_invoice_count: number
  outstanding_paise: number
  outstanding_display: string
  unbilled_charge_count: number
  unbilled_total_paise: number
  unbilled_total_display: string
  /**
   * Pending charges with NO configured price. They sit in `unbilled_total_paise`
   * (and `net_due_paise`) as 0 — while this is non-zero those totals are
   * floors, not facts, and must not be shown as the whole position.
   */
  unpriced_charge_count: number
  deposit_balance_paise: number
  deposit_balance_display: string
  open_claim_count: number
  net_due_paise: number
  net_due_display: string
}

export interface DischargeAdvisory extends FinancialSummary {
  advisory_status: 'clear' | 'outstanding' | 'unknown'
  unpriceable_event_count: number
  uncaptured_event_count: number
  reasons: string[]
  /** The deployment switch. False for the pilot, and it is the invariant. */
  enforced: boolean
  /** With `enforced` false this is ALWAYS false. Billing never stops care. */
  blocks_discharge: boolean
  headline: string
  message: string
  checked_at: string
}

export interface PrintView {
  invoice: Invoice
  supplier: {
    name: string
    name_is_configured: boolean
    address: string
    gstin: string
    notice: string
  }
  number_series_configured: boolean
  number_notice: string | null
  amount_in_words: string
  has_tax: boolean
  gst_note: string
  printed_at: string
}

export interface BillingConfig {
  module: string
  enabled: boolean
  discharge_financial_enforce: boolean
  default_tariff_plan: TariffPlan | null
  service_item_count: number
  priced_service_item_count: number
  unpriced_service_item_count: number
  financial_year: string
}

/** A refusal the server can explain: `rate_not_configured`, `no_charges`, … */
export interface BillingApiError extends Error {
  status?: number
  code?: string
  detail?: unknown
}

/* ── Calls ─────────────────────────────────────────────────────────────────── */

const post = (path: string, body?: unknown) =>
  apiFetch(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

export const getBillingConfig = () => apiFetch<BillingConfig>('/billing/config')

export const listServiceItems = (params: { search?: string; category?: string } = {}) => {
  const q = new URLSearchParams()
  if (params.search) q.set('search', params.search)
  if (params.category) q.set('category', params.category)
  const qs = q.toString()
  return apiFetch<{ items: ServiceItem[] }>(`/billing/service-items${qs ? `?${qs}` : ''}`)
}

export const upsertServiceItem = (item: {
  code: string
  name: string
  category?: string
  uom?: string
  gst_treatment?: GstTreatment
  gst_rate_bp?: number
  hsn_sac?: string | null
  /** null = no price. Do NOT send 0 to mean "unpriced". */
  default_rate_paise?: number | null
  is_active?: boolean
  /** New item: a code already on the list is refused (409) instead of overwritten. */
  create_only?: boolean
}) => post('/billing/service-items', item) as Promise<{ item: ServiceItem }>

export const listTariffPlans = () => apiFetch<{ plans: TariffPlan[] }>('/billing/tariff-plans')

export const upsertTariffPlan = (plan: {
  code: string; name: string; plan_type?: string; is_default?: boolean
}) => post('/billing/tariff-plans', plan) as Promise<{ plan: TariffPlan }>

export const listTariffRates = (tariffPlanId?: number) =>
  apiFetch<{ rates: TariffRate[] }>(
    `/billing/tariff-rates${tariffPlanId ? `?tariff_plan_id=${tariffPlanId}` : ''}`,
  )

export const setTariffRate = (rate: {
  tariff_plan_id: number
  service_code: string
  rate_paise: number
  ward_class?: string | null
  effective_from?: string | null
}) => post('/billing/tariff-rates', rate) as Promise<{ rate: TariffRate }>

export const listPackages = (tariffPlanId?: number) =>
  apiFetch<{ packages: PackageRate[] }>(
    `/billing/packages${tariffPlanId ? `?tariff_plan_id=${tariffPlanId}` : ''}`,
  )

export const upsertPackage = (pkg: {
  tariff_plan_id: number
  package_code: string
  name: string
  package_rate_paise: number
  ward_class?: string | null
  los_days_included?: number | null
}) => post('/billing/packages', pkg) as Promise<{ package: PackageRate }>

export const resolveRate = (serviceCode: string, tariffPlanId?: number) =>
  apiFetch<RateResolution>(
    `/billing/rate/${encodeURIComponent(serviceCode)}` +
    (tariffPlanId ? `?tariff_plan_id=${tariffPlanId}` : ''),
  )

export const listCharges = (patientId: string, admissionId?: string) =>
  apiFetch<{ patient_id: string; charges: Charge[]; summary: FinancialSummary }>(
    `/billing/patients/${encodeURIComponent(patientId)}/charges` +
    (admissionId ? `?admission_id=${encodeURIComponent(admissionId)}` : ''),
  )

export const postCharge = (patientId: string, charge: {
  service_code: string
  description?: string
  quantity_milli?: number
  unit_rate_paise?: number | null
  discount_paise?: number
  discount_bp?: number
  admission_id?: string | null
  service_date?: string | null
  notes?: string | null
}) => post(`/billing/patients/${encodeURIComponent(patientId)}/charges`, charge) as
  Promise<{ charge: Charge }>

export const captureClinicalCharges = (
  patientId: string, body: { admission_id?: string | null; dry_run?: boolean } = {},
) => post(`/billing/patients/${encodeURIComponent(patientId)}/charges/capture`, body) as
  Promise<CaptureResult & { status: string }>

export const postBedDays = (patientId: string, body: {
  admission_id: string; service_code: string; through?: string | null
}) => post(`/billing/patients/${encodeURIComponent(patientId)}/bed-days`, body) as
  Promise<{ posted_count: number; posted_total_display: string; unpriced_days: string[] }>

export const cancelCharge = (chargeId: number, reason: string) =>
  post(`/billing/charges/${chargeId}/cancel`, { reason }) as Promise<{ charge: Charge }>

export const listInvoices = (params: { patient_id?: string; status?: string } = {}) => {
  const q = new URLSearchParams()
  if (params.patient_id) q.set('patient_id', params.patient_id)
  if (params.status) q.set('status', params.status)
  const qs = q.toString()
  return apiFetch<{ invoices: Invoice[] }>(`/billing/invoices${qs ? `?${qs}` : ''}`)
}

export const getInvoice = (invoiceId: number) =>
  apiFetch<{ invoice: Invoice }>(`/billing/invoices/${invoiceId}`)

export const createInvoice = (patientId: string, body: {
  admission_id?: string | null
  invoice_type?: 'interim' | 'final' | 'advance'
  charge_ids?: number[]
  interstate?: boolean
  issue?: boolean
  notes?: string | null
}) => post(`/billing/patients/${encodeURIComponent(patientId)}/invoices`, body) as
  Promise<{ invoice: Invoice }>

export const createOpdInvoice = (patientId: string, body: {
  lines: { service_code: string; quantity_milli?: number; description?: string }[]
  issue?: boolean
}) => post(`/billing/patients/${encodeURIComponent(patientId)}/opd-invoice`, body) as
  Promise<{ invoice: Invoice; charge_ids: number[] }>

export const issueInvoice = (invoiceId: number) =>
  post(`/billing/invoices/${invoiceId}/issue`) as Promise<{ invoice: Invoice }>

/** Cancellation ALWAYS carries a reason — the server refuses a blank one. */
export const cancelInvoice = (invoiceId: number, reason: string) =>
  post(`/billing/invoices/${invoiceId}/cancel`, { reason }) as Promise<{ invoice: Invoice }>

export const creditNote = (invoiceId: number, reason: string) =>
  post(`/billing/invoices/${invoiceId}/credit-note`, { reason }) as Promise<{ invoice: Invoice }>

export const getPrintView = (invoiceId: number) =>
  apiFetch<PrintView>(`/billing/invoices/${invoiceId}/print`)

export const getDischargeAdvisory = (patientId: string, admissionId?: string) =>
  apiFetch<DischargeAdvisory>(
    `/billing/patients/${encodeURIComponent(patientId)}/discharge-advisory` +
    (admissionId ? `?admission_id=${encodeURIComponent(admissionId)}` : ''),
  )

export const getPatientSummary = (patientId: string, admissionId?: string) =>
  apiFetch<FinancialSummary>(
    `/billing/patients/${encodeURIComponent(patientId)}/summary` +
    (admissionId ? `?admission_id=${encodeURIComponent(admissionId)}` : ''),
  )

/** Pull the server's machine-readable refusal code out of a thrown apiFetch error. */
export function billingErrorCode(err: unknown): string | null {
  const detail = (err as { detail?: unknown })?.detail as { error?: string } | undefined
  if (detail && typeof detail === 'object' && typeof detail.error === 'string') {
    return detail.error
  }
  return null
}
