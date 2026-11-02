import { apiFetch } from './client'
import type { OperationEvent } from './feedback'

export interface MortuarySlot { id: string; code: string; name: string; active: number; occupied: boolean }
export interface MortuaryCase {
  id: string; patient_id: string | null; body_tag: string; identity_label: string; authority_reference: string
  received_at: string; slot_id: string | null; status: string; hold_reason: string | null; version: number
  release_authority_reference: string | null; recipient_name: string | null; recipient_relationship: string | null
  identity_check_reference: string | null; witness_id: string | null; released_at: string | null; events?: OperationEvent[]
}
export interface MortuaryRegister { cases: MortuaryCase[]; slots: MortuarySlot[]; limit: number; offset: number; notice: string; has_more: boolean }
export interface MortuaryIntake {
  body_tag: string; identity_label: string; authority_reference: string; slot_id: string; patient_id?: string | null; received_at?: string | null
}
export interface MortuaryAction {
  action: string; version: number; note: string; slot_id?: string; authority_reference?: string
  release_authority_reference?: string; recipient_name?: string; recipient_relationship?: string
  identity_check_reference?: string; witness_id?: string
}
export const loadMortuary = (offset = 0) => apiFetch<MortuaryRegister>(`/mortuary?limit=100&offset=${offset}`)
export const getMortuaryCase = (id: string) => apiFetch<MortuaryCase>(`/mortuary/${encodeURIComponent(id)}`)
export const addMortuarySlot = (body: {code: string; name: string}) => apiFetch<MortuarySlot>('/mortuary/slots', { method: 'POST', body: JSON.stringify(body) })
export const recordMortuaryIntake = (body: MortuaryIntake, key: string) => apiFetch<MortuaryCase>('/mortuary', {
  method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify(body),
})
export const actOnMortuaryCase = (id: string, body: MortuaryAction) => apiFetch<MortuaryCase>(`/mortuary/${encodeURIComponent(id)}/actions`, {
  method: 'POST', body: JSON.stringify(body),
})
