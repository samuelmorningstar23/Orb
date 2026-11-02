import { apiFetch } from './client'

export interface OperationEvent { id: string; action: string; note: string; actor_id: string; occurred_at: string }
export interface FeedbackCase {
  id: string; patient_id: string | null; kind: 'feedback' | 'complaint'; channel: string
  subject: string; statement: string; reporter_name: string | null; contact_reference: string | null
  rating: number | null; owner_id: string | null; status: string; resolution: string | null
  created_at: string; updated_at: string; version: number; events?: OperationEvent[]
}
export interface FeedbackInput {
  kind: 'feedback' | 'complaint'; channel: string; subject: string; statement: string
  patient_id?: string | null; reporter_name?: string | null; contact_reference?: string | null; rating?: number | null
}
export interface FeedbackRegister { cases: FeedbackCase[]; limit: number; offset: number; notice: string; has_more: boolean }
export const loadFeedback = (offset = 0) => apiFetch<FeedbackRegister>(`/feedback?limit=100&offset=${offset}`)
export const getFeedback = (id: string) => apiFetch<FeedbackCase>(`/feedback/${encodeURIComponent(id)}`)
export const recordFeedback = (body: FeedbackInput, key: string) => apiFetch<FeedbackCase>('/feedback', {
  method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify(body),
})
export const actOnFeedback = (id: string, body: { action: string; version: number; note: string; owner_id?: string }) =>
  apiFetch<FeedbackCase>(`/feedback/${encodeURIComponent(id)}/actions`, { method: 'POST', body: JSON.stringify(body) })
