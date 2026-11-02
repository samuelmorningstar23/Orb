/** Locally entered documentation; these calls do not order or execute treatment. */
import { apiFetch } from './client'

export type ClinicalDomain = 'radiology' | 'care-plans' | 'emergency' | 'perioperative' | 'dialysis'
export interface Observation {
  at: string
  blood_pressure?: string | null
  pulse?: number | null
  respiratory_rate?: number | null
  spo2?: number | null
  temperature_c?: number | null
  note?: string
}
export interface CareTask {
  id: string; description: string; owner: string; due_at?: string | null
  state: 'planned' | 'done' | 'not_done'; completed_at?: string | null; evaluation?: string
}
export interface DialysisPrescription {
  modality: string; duration_minutes: number; target_fluid_ml?: number | null
  blood_flow_ml_min?: number | null; dialysate?: string | null; anticoagulation?: string; instructions?: string
}
export interface RisData {
  modality: 'X-ray' | 'CT' | 'MRI' | 'Ultrasound' | 'Other'; body_site: string
  indication?: string; priority?: 'routine' | 'urgent' | 'stat'
  scheduled_at?: string | null; arrived_at?: string | null; performed_at?: string | null
  performer?: string | null; findings?: string; impression?: string; order_id?: string | null; accession?: string | null
}
export interface CareData {
  problem: string; goal: string; review_at?: string | null; tasks?: CareTask[]
  evaluation?: string; closure_reason?: string
}
export interface EmergencyData {
  arrival_at: string; complaint: string; triage_category?: string | null; triaged_at?: string | null
  triage_note?: string; seen_at?: string | null; clinician?: string | null; assessment?: string
  observations?: Observation[]; disposition?: string | null; disposition_at?: string | null; disposition_note?: string
}
export interface PerioperativeData {
  record_type: 'anaesthesia' | 'pacu' | 'implant'; started_at?: string | null; ended_at?: string | null
  technique?: string | null; assessment?: string; airway?: string; observations?: Observation[]; handover?: string
  device_name?: string | null; manufacturer?: string | null; model?: string | null; lot?: string | null
  serial?: string | null; expiry_date?: string | null; site?: string | null; implanted_at?: string | null; notes?: string
}
export interface DialysisData {
  scheduled_at?: string | null; started_at?: string | null; ended_at?: string | null; indication?: string
  access: string; prescription?: DialysisPrescription | null; actual_duration_minutes?: number | null
  actual_fluid_ml?: number | null; observations?: Observation[]; complications?: string; handover?: string
}
export type ClinicalData = RisData | CareData | EmergencyData | PerioperativeData | DialysisData
export interface ClinicalHistory {
  version: number; kind: string; prior_state: string | null; new_state: string; actor_id: string
  recorded_at: string; reason: string | null; snapshot: { text?: string; title?: string; data?: ClinicalData }
}
export interface ClinicalRecord {
  id: string; patient_id: string; admission_id: string | null; surgery_id: string | null
  facility_id: number; department: string | null; title: string; state: string; data: ClinicalData
  version: number; created_by: string; updated_by: string; created_at: string; updated_at: string
  signed_by: string | null; signed_at: string | null; patient_name?: string; mrn?: string; history?: ClinicalHistory[]
  history_next_offset?: number | null
}
export interface ClinicalMeta {
  module: ClinicalDomain; states: string[]; initial_state: string; terminal_states: string[]
  can_write: boolean; can_sign: boolean; can_prescribe: boolean; documentation_only: true
}
export interface CreateClinicalRecord {
  patient_id: string; admission_id?: string | null; surgery_id?: string | null; title: string; data: ClinicalData
}
export interface SurgicalCaseChoice { id: string; patient_id: string; patient_name: string; procedure_name: string; scheduled_date: string }
export const getClinicalMeta = (domain: ClinicalDomain) => apiFetch<ClinicalMeta>(`/${domain}/meta`)
export const listClinicalRecords = (domain: ClinicalDomain, patientId?: string, offset = 0) =>
  apiFetch<{ records: ClinicalRecord[]; limit: number; offset: number }>(`/${domain}/records?offset=${offset}${patientId ? `&patient_id=${encodeURIComponent(patientId)}` : ''}`)
export const getClinicalRecord = (domain: ClinicalDomain, id: string) => apiFetch<ClinicalRecord>(`/${domain}/records/${encodeURIComponent(id)}`)
export const createClinicalRecord = (domain: ClinicalDomain, body: CreateClinicalRecord, key: string) =>
  apiFetch<ClinicalRecord>(`/${domain}/records`, { method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify(body) })
export const updateClinicalRecord = (domain: ClinicalDomain, id: string, body: { version: number; title: string; state: string; data: ClinicalData; reason?: string }) =>
  apiFetch<ClinicalRecord>(`/${domain}/records/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(body) })
export const signClinicalRecord = (domain: ClinicalDomain, id: string, version: number) =>
  apiFetch<ClinicalRecord>(`/${domain}/records/${encodeURIComponent(id)}/sign`, { method: 'POST', body: JSON.stringify({ version }) })
export const addClinicalAddendum = (domain: ClinicalDomain, id: string, version: number, text: string, reason: string) =>
  apiFetch<ClinicalRecord>(`/${domain}/records/${encodeURIComponent(id)}/addenda`, { method: 'POST', body: JSON.stringify({ version, text, reason }) })
export const listPerioperativeCases = (q = '', offset = 0) => apiFetch<{ cases: SurgicalCaseChoice[] }>(`/perioperative/cases?q=${encodeURIComponent(q)}&offset=${offset}`)
export const getClinicalHistory = (domain: ClinicalDomain, id: string, offset: number) =>
  apiFetch<{ history: ClinicalHistory[]; history_next_offset: number | null }>(`/${domain}/records/${encodeURIComponent(id)}/history?offset=${offset}`)
