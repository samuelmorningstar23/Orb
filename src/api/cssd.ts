import { apiFetch } from './client'
import type { OperationEvent } from './feedback'

export interface CssdSet { id: string; code: string; name: string; active: number; items: { name: string; quantity: number }[] }
export interface CssdIssue { id: string; surgery_id: string | null; patient_id: string | null; destination: string; reference: string; issued_at: string; returned_at: string | null }
export interface CssdPack {
  id: string; tag: string; set_id: string; status: string; cycle_id: string | null; expires_at: string | null
  version: number; available_to_issue: boolean; events?: OperationEvent[]; issues?: CssdIssue[]
}
export interface CssdCycle {
  id: string; reference: string; sterilizer_reference: string; sop_reference: string; method: string; status: string; version: number
  started_at: string; completed_at: string | null; physical_result: string | null; chemical_result: string | null
  biological_result: string | null; biological_basis: string | null; measurements: string | null
  completion_note: string | null; release_reference: string | null; released_at: string | null
  packs?: CssdPack[]; events?: OperationEvent[]
}
export interface CssdRegister { sets: CssdSet[]; packs: CssdPack[]; cycles: CssdCycle[]; limit: number; offset: number; notice: string; has_more: boolean }
export interface CssdPackAction { action: string; version: number; note: string; destination?: string; reference?: string; surgery_id?: string | null }
export interface CssdCycleAction {
  action: string; version: number; note: string; physical_result?: string; chemical_result?: string
  biological_result?: string; biological_basis?: string | null; measurements?: string; expires_at?: string; release_reference?: string
}
const post = <T>(path: string, body: unknown, key?: string) => apiFetch<T>(`/cssd${path}`, {
  method: 'POST', ...(key ? { headers: { 'Idempotency-Key': key } } : {}), body: JSON.stringify(body),
})
export const loadCssd = (offset = 0) => apiFetch<CssdRegister>(`/cssd?limit=100&offset=${offset}`)
export const getCssdPack = (id: string) => apiFetch<CssdPack>(`/cssd/packs/${encodeURIComponent(id)}`)
export const getCssdCycle = (id: string) => apiFetch<CssdCycle>(`/cssd/cycles/${encodeURIComponent(id)}`)
export const createCssdSet = (body: { code: string; name: string; items: { name: string; quantity: number }[] }) => post<CssdSet>('/sets', body)
export const createCssdPack = (body: {set_id: string; tag: string}) => post<CssdPack>('/packs', body)
export const actOnCssdPack = (id: string, body: CssdPackAction) => post<CssdPack>(`/packs/${encodeURIComponent(id)}/actions`, body)
export const startCssdCycle = (body: {reference: string; sterilizer_reference: string; sop_reference: string; method: string; pack_ids: string[]}, key: string) => post<CssdCycle>('/cycles', body, key)
export const actOnCssdCycle = (id: string, body: CssdCycleAction) => post<CssdCycle>(`/cycles/${encodeURIComponent(id)}/actions`, body)
