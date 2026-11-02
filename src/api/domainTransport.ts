import { apiFetch, apiFetchText } from './client'

type Detail = Record<string, unknown>
type ErrorFactory = (message: string, status: number, code: string, detail: Detail) => Error

/** Retain module-specific refusals without duplicating session or CSRF logic. */
export async function domainRequest<T>(path: string, options: RequestInit, errorFactory: ErrorFactory,
  format: 'json' | 'text' = 'json'): Promise<T> {
  try {
    return format === 'text' ? await apiFetchText(path, options) as T : await apiFetch<T>(path, options)
  } catch (failure) {
    if (!(failure instanceof Error) || !('status' in failure) || typeof failure.status !== 'number') throw failure
    const source = failure as Error & { status: number; detail?: unknown; permissions?: unknown; mfaEnrollTicket?: string }
    const raw = source.detail ?? source.permissions
    const detail = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Detail : {}
    const code = typeof detail.error === 'string' ? detail.error : `http_${source.status}`
    const mapped = errorFactory(source.message, source.status, code, detail)
    // Consumers retain validation arrays, permission details and enrolment tickets.
    Object.assign(mapped, { detail: raw })
    if (source.permissions !== undefined) Object.assign(mapped, { permissions: source.permissions })
    if (source.mfaEnrollTicket) Object.assign(mapped, { mfaEnrollTicket: source.mfaEnrollTicket })
    throw mapped
  }
}
