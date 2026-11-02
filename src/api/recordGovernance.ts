import { apiFetch } from './client'

export type RecordKind = 'hold' | 'file' | 'release'
export interface GovernanceEvent { id: string; action: string; reason: string; occurred_at: string; actor_id: string }
export interface GovernanceRecord {
  id: string; patient_id: string; version: number; created_at: string
  reason?: string; authority_reference?: string; categories_json?: string; released_at?: string
  file_number?: string; medium?: string; location?: string; custodian?: string; state?: string
  purpose?: string; recipient?: string; documents_json?: string; status?: string
  events?: GovernanceEvent[]
}
export interface CreateGovernanceRecord {
  patient_id: string; categories?: string[]; reason?: string; authority_reference?: string
  file_number?: string; medium?: 'paper' | 'digital_reference'; location?: string; custodian?: string
  purpose?: string; recipient?: string; documents?: string[]
}
export interface GovernanceTransition {
  version: number; action: string; reason: string; location?: string; custodian?: string; reference?: string
}
export const listGovernance = (kind: RecordKind, patientId: string, offset: number) =>
  apiFetch<{ records: GovernanceRecord[]; total: number }>(`/record-governance/${kind}?patient_id=${encodeURIComponent(patientId)}&limit=25&offset=${offset}`)
export const getGovernance = (kind: RecordKind, id: string) =>
  apiFetch<GovernanceRecord>(`/record-governance/${kind}/${encodeURIComponent(id)}`)
export const createGovernance = (kind: RecordKind, values: CreateGovernanceRecord) =>
  apiFetch<GovernanceRecord>(`/record-governance/${kind}`, { method: 'POST', body: JSON.stringify(values) })
export const transitionGovernance = (kind: RecordKind, id: string, values: GovernanceTransition) =>
  apiFetch<GovernanceRecord>(`/record-governance/${kind}/${encodeURIComponent(id)}/transition`, { method: 'POST', body: JSON.stringify(values) })
