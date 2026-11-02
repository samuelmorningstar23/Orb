import { apiFetch } from './client'
import type { OperationEvent } from './feedback'

export interface InfectionDefinition { id: string; code: string; name: string; case_definition: string; counting_policy: string; denominator_unit: 'patient_days' | 'device_days'; device_label: string | null; evidence_reference: string }
export interface InfectionWindow { id: string; definition_id: string; label: string; population_description: string; start_date: string; end_date: string; source_reference: string; rate_multiplier: number; minimum_cell: number; isolate_policy: 'all_isolates' | 'first_per_patient'; status: 'open' | 'finalized'; version: number; finalization_note: string | null; events?: OperationEvent[] }
export interface InfectionCase { id: string; window_id: string; patient_id: string; episode_reference: string; event_date: string; evidence_reference: string; statement: string; status: 'under_review' | 'included' | 'excluded'; classification_reason: string | null; reviewer_reference: string | null; version: number }
export interface InfectionExposure { id: string; window_id: string; patient_id: string; observed_date: string; device_reference: string; day_count: 1; evidence_reference: string; status: string; void_reason: string | null; version: number }
export interface InfectionSusceptibility { id: string; window_id: string; patient_id: string; isolate_reference: string; specimen_date: string; organism_code: string; specimen_code: string; antibiotic_code: string; result: 'S' | 'I' | 'R'; standard_reference: string; method: string; evidence_reference: string; validator_reference: string; status: string; void_reason: string | null; version: number }
export interface InfectionIsolation { id: string; patient_id: string; case_id: string | null; precautions: string; started_at: string; start_reason: string; authority_reference: string; status: 'active' | 'ended'; ended_at: string | null; end_reason: string | null; end_authority_reference: string | null; version: number; events?: OperationEvent[] }
export interface InfectionWindowDetail extends InfectionWindow { definition: InfectionDefinition; cases: InfectionCase[]; exposures: InfectionExposure[]; susceptibility: InfectionSusceptibility[]; limit: number; offset: number; has_more: boolean }
export interface InfectionRegister { definitions: InfectionDefinition[]; windows: InfectionWindow[]; isolations: InfectionIsolation[]; offset: number; limit: number; has_more: boolean; notice: string }
export interface InfectionInputs {
  definition: Omit<InfectionDefinition, 'id'>
  window: Omit<InfectionWindow, 'id' | 'status' | 'version' | 'finalization_note' | 'events'>
  case: Pick<InfectionCase, 'window_id' | 'patient_id' | 'episode_reference' | 'event_date' | 'evidence_reference' | 'statement'>
  exposure: Pick<InfectionExposure, 'window_id' | 'patient_id' | 'observed_date' | 'evidence_reference'> & {device_reference?: string | null; day_count?: 1}
  susceptibility: Omit<InfectionSusceptibility, 'id' | 'status' | 'void_reason' | 'version'>
  isolation: Pick<InfectionIsolation, 'patient_id' | 'precautions' | 'started_at' | 'start_reason' | 'authority_reference'> & {case_id?: string | null}
}
export interface InfectionMetrics {
  window_id: string; definition: InfectionDefinition; population: string; start_date: string; end_date: string; minimum_cell: number
  isolate_policy: string; group_by: string[]; notice: string
  rate: {suppressed: boolean; numerator: number | null; denominator: number | null; multiplier: number; value: number | null}
  antibiogram: {organism: string; antibiotic: string; specimen: string | null; standard_reference: string; method: string; suppressed: boolean; tested: number | null; counts: {S: number; I: number; R: number} | null; susceptible_percent: number | null}[]
}
const base = '/infection-surveillance'
const post = <T>(path: string, body: unknown, key?: string) => apiFetch<T>(base + path, { method: 'POST', body: JSON.stringify(body), ...(key ? {headers: {'Idempotency-Key': key}} : {}) })
export const loadInfectionSurveillance = (offset = 0) => apiFetch<InfectionRegister>(`${base}?limit=100&offset=${offset}`)
export const getInfectionWindow = (id: string, offset = 0) => apiFetch<InfectionWindowDetail>(`${base}/records/window/${encodeURIComponent(id)}?limit=100&offset=${offset}`)
export const getInfectionIsolation = (id: string) => apiFetch<InfectionIsolation>(`${base}/records/isolation/${encodeURIComponent(id)}`)
export const createInfectionRecord = <K extends keyof InfectionInputs>(kind: K, body: InfectionInputs[K], key: string) => post<{id: string}>(`/records/${kind}`, body, key)
export const classifyInfectionCase = (id: string, body: {version: number; status: InfectionCase['status']; reason: string; reviewer_reference: string}) => post<InfectionCase>(`/cases/${encodeURIComponent(id)}/classification`, body)
export const voidInfectionRecord = (kind: 'exposure' | 'susceptibility', id: string, body: {version: number; reason: string}) => post<{id: string}>(`/records/${kind}/${encodeURIComponent(id)}/void`, body)
export const endInfectionIsolation = (id: string, body: {version: number; ended_at: string; reason: string; authority_reference: string}) => post<InfectionIsolation>(`/isolations/${encodeURIComponent(id)}/end`, body)
export const actOnInfectionWindow = (id: string, body: {version: number; action: 'finalize' | 'reopen'; reason: string}) => post<InfectionWindow>(`/windows/${encodeURIComponent(id)}/actions`, body)
export const getInfectionMetrics = (id: string, grouping: string) => apiFetch<InfectionMetrics>(`${base}/windows/${encodeURIComponent(id)}/metrics?group_by=${encodeURIComponent(grouping)}`)
