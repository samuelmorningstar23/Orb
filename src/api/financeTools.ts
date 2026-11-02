import { apiFetch } from './client'
import { workbenchDownload } from './localWorkbenchDownload'

export interface FinancePeriod { facility_id: number; from_day: string; to_day: string }
export interface AccountMapping { facility_id: number; account_role: string; account_code: string; account_name: string }
export interface PayoutRuleInput {
  facility_id: number; doctor_id: string; selector_type: 'service' | 'category' | 'all'; selector_value: string
  mode: 'percentage' | 'fixed'; basis: 'gross_ex_tax' | 'net_ex_tax'; recognition: 'billed' | 'fully_collected'
  percent_bp?: number; fixed_paise?: number; effective_from: string; effective_to?: string
}
export interface PayoutRule extends PayoutRuleInput { id: number; is_active: number; created_at: string }
export interface FinanceMeta {
  facility_id: number; account_roles: { id: string; label: string }[]
  mappings: AccountMapping[]; doctors: { id: string; name: string }[]; rules: PayoutRule[]
  limits: { days: number; sources: number; lines: number }; notes: string[]
}
export interface FinanceIssue { code: string; account_role?: string; entry_key?: string; source_line_id?: number; charge_id?: number | null; doctor_id?: string }
export interface JournalRow {
  entry_key: string; line_no: number; occurred_at: string; source_type: string; source_id: number; source_number: string
  patient_id: string; supplier_facility_id: number | null; supplier_legal_name: string | null
  supplier_gstin: string | null; supplier_snapshot_at: string | null; account_role: string
  account_code: string | null; account_name: string | null; debit_paise: number; credit_paise: number; source_notice: string | null
}
export interface JournalPreview extends FinancePeriod {
  rows: JournalRow[]; issues: FinanceIssue[]; debit_paise: number; credit_paise: number; balanced: boolean; can_export: boolean
  mappings: Record<string, AccountMapping>
}
export interface ChargeAttribution {
  charge_id: number; doctor_id: string; doctor_name?: string; reason: string; version: number; created_at: string; updated_at: string
}
export interface FinanceCharge {
  id: number; patient_id: string; patient_name: string; service_code: string; description: string; status: string
  service_date: string | null; gross_paise: number; discount_paise: number; total_paise: number; invoice_id: number | null
  attribution: ChargeAttribution | null
}
export interface PayoutLine {
  source_line_id: number; patient_id: string; doctor_id: string; attribution_version: number; rule_id: number
  source_kind: 'earning' | 'credit'; basis_paise: number; amount_paise: number
  snapshot: { invoice_line: { invoice_number: string; service_code: string; description: string; [key: string]: unknown }; rule: PayoutRule; [key: string]: unknown }
}
export interface PayoutPreview extends FinancePeriod {
  doctor_id: string | null; lines: PayoutLine[]; issues: FinanceIssue[]; excluded: FinanceIssue[]
  total_paise: number; preview_hash: string; can_create: boolean
}
export interface PayoutStatement extends FinancePeriod {
  id: string; doctor_id: string | null; status: 'draft' | 'approved' | 'paid' | 'cancelled'
  total_paise: number; deduction_paise: number; net_paise: number; deduction_reason: string | null
  preview_hash: string; created_by: string; created_at: string
  approved_by: string | null; approved_at: string | null; paid_by: string | null; paid_at: string | null
  payment_reference: string | null; cancelled_by: string | null; cancelled_at: string | null
  lines?: PayoutLine[]; replayed?: boolean
}
const post = <T,>(path: string, body?: unknown) => apiFetch<T>(`/finance-tools${path}`, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
export const getFinanceMeta = (facilityId = 1) => apiFetch<FinanceMeta>(`/finance-tools/meta?facility_id=${facilityId}`)
export const saveAccountMapping = (body: AccountMapping) => apiFetch<AccountMapping>('/finance-tools/account-mappings', { method: 'PUT', body: JSON.stringify(body) })
export const previewJournal = (period: FinancePeriod) => post<JournalPreview>('/journal/preview', period)
export const createJournalExport = (period: FinancePeriod, key: string) => post<{ id: string; content_sha256: string; replayed: boolean }>('/journal/exports', { ...period, idempotency_key: key })
export const downloadJournal = (id: string) => workbenchDownload(`/finance-tools/journal/exports/${encodeURIComponent(id)}.csv`, 'orb-journal.csv')
export const getFinanceCharges = (period: FinancePeriod) => apiFetch<{ charges: FinanceCharge[] }>(`/finance-tools/charges?${new URLSearchParams({ facility_id: String(period.facility_id), from_day: period.from_day, to_day: period.to_day })}`)
export const attributeFinanceCharge = (chargeId: number, body: { facility_id: number; doctor_id: string; reason: string; version?: number }) => apiFetch<ChargeAttribution>(`/finance-tools/charges/${chargeId}/doctor`, { method: 'PUT', body: JSON.stringify(body) })
export const createPayoutRule = (body: PayoutRuleInput) => post<PayoutRule>('/payout-rules', body)
export const retirePayoutRule = (id: number) => post<{ id: number; is_active: number }>(`/payout-rules/${id}/retire`)
export const previewPayout = (period: FinancePeriod, doctorId?: string) => post<PayoutPreview>('/payouts/preview', { ...period, ...(doctorId ? { doctor_id: doctorId } : {}) })
export const getPayoutStatements = async (facilityId = 1) => {
  const response = await apiFetch<{ statements: PayoutStatement[] }>(`/finance-tools/payout-statements?facility_id=${facilityId}`)
  return response.statements
}
export const getPayoutStatement = (id: string) => apiFetch<PayoutStatement>(`/finance-tools/payout-statements/${encodeURIComponent(id)}`)
export const createPayoutStatement = (body: FinancePeriod & { idempotency_key: string; doctor_id?: string; deduction_paise: number; deduction_reason?: string }) => post<PayoutStatement>('/payout-statements', body)
export const approvePayoutStatement = (id: string) => post<PayoutStatement>(`/payout-statements/${encodeURIComponent(id)}/approve`)
export const markPayoutPaid = (id: string, reference: string) => post<PayoutStatement>(`/payout-statements/${encodeURIComponent(id)}/paid`, { payment_reference: reference })
export const cancelPayoutStatement = (id: string) => post<PayoutStatement>(`/payout-statements/${encodeURIComponent(id)}/cancel`)
export const downloadPayoutStatement = (id: string) => workbenchDownload(`/finance-tools/payout-statements/${encodeURIComponent(id)}.csv`, 'orb-doctor-payout.csv')

export interface JournalExportSummary extends FinancePeriod {
  id: string; debit_paise: number; credit_paise: number; content_sha256: string; created_by: string; created_at: string
}
export const getJournalExports = (facilityId = 1) => apiFetch<JournalExportSummary[]>(`/finance-tools/journal/exports?facility_id=${facilityId}`)
