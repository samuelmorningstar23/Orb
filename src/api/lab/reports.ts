/**
 * Orb, Laboratory API: reports (lis-14).
 *
 * One report is one sample (one accession). It carries released values only:
 * a test the laboratory has not authorised is 'pending' with no value. Every
 * read and every print is audited on the server, so a screen loads a report
 * when asked and never polls.
 *
 * Times are canonical UTC instants; print them through src/lib/datetime.ts so
 * they read in hospital time.
 */
import { labGet } from './index'

export type ReportStatus = 'final' | 'amended' | 'interim'
export type ReportItemState = 'reported' | 'pending' | 'rejected' | 'cancelled' | 'withdrawn'

export interface ReportPerson {
  name: string
}

export interface ReportSignatory extends ReportPerson {
  registration_number: string | null
  /** The number, or 'Registration number not on file'. */
  registration_text: string
}

export interface ReportResult {
  id: number
  version: number
  /** As the bench typed it; a bound keeps its comparator ('<0.01'). */
  value_text: string | null
  comparator: string | null
  unit: string | null
  abnormal_flag: string
  /** Words for H, L, HH, LL or A; empty when there is no flag. */
  flag_text: string
  is_critical: number
  /** The range snapshot taken when the result was entered. */
  reference_range_text: string | null
  /** False for a coded or text result, which has no range. */
  range_applies: boolean
  comment: string | null
  amend_reason: string | null
  reported_at: string | null
  authorised_by: ReportSignatory | null
  checked_by: ReportPerson | null
  checked_at: string | null
  simulated: boolean
}

export interface ReportVersion {
  version: number
  state: 'replaced' | 'withdrawn'
  /** True for a withdrawn version, and for every replaced version of a test
   *  whose result was later withdrawn (the chain may be another patient's
   *  sample): the server then sends no value, unit, flag, range or
   *  amendment reason, only who and when. */
  value_withheld?: boolean
  /** Never sent for a withdrawn or withheld version. */
  value_text: string | null
  unit: string | null
  flag_text: string
  reference_range_text: string | null
  reported_at: string | null
  reported_by: ReportPerson | null
  ended_at: string | null
  ended_by: ReportPerson | null
  reason: string | null
}

export interface ReportItem {
  item_id: number
  order_id: string
  test: { id: string; name: string; short_name: string; result_type: string }
  panel_name: string | null
  section_id: string | null
  section_name: string | null
  state: ReportItemState
  state_text: string
  result: ReportResult | null
  history: ReportVersion[]
  amended: boolean
  cancelled: { reason: string | null; by: ReportPerson | null; at: string | null } | null
}

export interface LabReport {
  accession: string
  status: ReportStatus
  status_text: string
  amended: boolean
  simulated: boolean
  pending_count: number
  patient: {
    id: string
    name: string | null
    mrn: string | null
    sex_text: string
    age_text: string
    ward: string | null
    bed: string | null
    department: string | null
    discharged: boolean
  }
  specimen: {
    type: string | null
    container: string | null
    priority: string
    priority_text: string
    status: string | null
    collected_at: string | null
    collected_by: ReportPerson | null
    collection_location: string | null
    received_at: string | null
    received_by: ReportPerson | null
  }
  requested_by: ReportPerson[]
  reported_at: string | null
  last_reported_at: string | null
  items: ReportItem[]
  signatories: ReportSignatory[]
  technologists: ReportPerson[]
  notes: string[]
  simulated_text: string
  /** Present on the print answer only. */
  printed_at?: string
  printed_by?: ReportPerson
  hospital?: { name: string | null }
}

export interface ReportSummary {
  accession: string
  status: ReportStatus
  status_text: string
  amended: boolean
  simulated: boolean
  pending_count: number
  patient: { id: string; name: string | null; mrn: string | null; ward: string | null; bed: string | null }
  specimen_type: string | null
  priority: string
  collected_at: string | null
  reported_at: string | null
  last_reported_at: string | null
  tests: string[]
  flagged_count: number
}

export interface ReportList {
  reports: ReportSummary[]
  day: string | null
  uhid: string | null
  cap: number
  scope: { department: string | null; hospital_wide: boolean }
}

/** Spaces a person typed or a scanner sent are left for the server to strip. */
function accessionPath(accession: string): string {
  return encodeURIComponent(accession.replace(/\s+/g, ''))
}

/** One sample's report (audited as viewed). */
export async function getLabReport(accession: string): Promise<LabReport> {
  const out = await labGet<{ report: LabReport }>(`/reports/${accessionPath(accession)}`)
  return out.report
}

/** The report as it is printed (audited as printed), with when and by whom. */
export async function printLabReport(accession: string): Promise<LabReport> {
  const out = await labGet<{ report: LabReport }>(`/reports/${accessionPath(accession)}/print`)
  return out.report
}

/** The laboratory's list: one hospital day, or one patient by exact UHID. */
export function listLabReports(params: { day?: string; uhid?: string }): Promise<ReportList> {
  return labGet<ReportList>('/reports', { day: params.day, uhid: params.uhid })
}

/** Every report of one patient, for the chart (ward-scoped on the server). */
export function listPatientLabReports(patientId: string): Promise<{
  patient_id: string
  reports: ReportSummary[]
  cap: number
}> {
  return labGet(`/reports/patient/${encodeURIComponent(patientId)}`)
}

/** The value as the report prints it: the value and its unit. */
export function reportValueText(result: Pick<ReportResult, 'value_text' | 'unit'>): string {
  const value = result.value_text ?? ''
  return result.unit && value ? `${value} ${result.unit}` : value
}
