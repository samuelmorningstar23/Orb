/**
 * Orb, Beds and admissions API: the shared base for every /api/adt call
 * (adt-4a).
 *
 * THIS FOLDER GROWS ONE FILE PER AREA. The bed board uses ./board.ts; bed
 * setup, moves, leave and absconded, end of stay, reservations and the census
 * each arrive as their own file beside it, written by the increment that
 * builds that area, importing the helpers below and adding nothing to this
 * file.
 *
 * THE MODULE IS OPT-IN (ORB_ADT, off unless a site turns it on). Every route
 * answers 404 `module_disabled` while it is off, which a screen treats as a
 * whole-panel state and never as an empty board.
 *
 * WHY A TYPED ERROR. The server's machine-readable `error` code is kept on
 * `AdtApiError.code` (state_changed, bed_occupied, read_back_mismatch,
 * boarder_acknowledgement_required and so on) so a screen branches on the
 * code, never on English. The calls go through `apiFetch`, so every Beds
 * screen gets the same session refresh, cookie and CSRF handling as every
 * other screen.
 */
import { apiFetch, errorDetailMessage } from '../client'

/** The server router's prefix under /api. */
export const ADT_API_PREFIX = '/adt'

/** The module id on /api/health and in the shell (HospitalModuleId). */
export const ADT_MODULE_ID = 'adt' as const

const UNREACHABLE_MESSAGE =
  'Could not reach the bed board. Check the connection and try again.'

/** A refusal from the Beds and admissions API, with the server's code intact. */
export class AdtApiError extends Error {
  /** HTTP status, or 0 when no reply arrived at all. */
  readonly status: number
  /** The server's `detail.error`, else `http_<status>`, else `network_error`. */
  readonly code: string
  /** The server's structured `detail`, untouched. */
  readonly detail: unknown

  constructor(message: string, status: number, code: string, detail: unknown = null) {
    super(message)
    this.name = 'AdtApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }

  /** Beds and admissions is switched off at this site. */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled'
  }

  /** No such record here, or not one this account may see. */
  get isNotFound(): boolean {
    return this.status === 404 && !this.isModuleDisabled
  }

  /** The caller's role does not hold the permission. */
  get isForbidden(): boolean {
    return this.status === 403
  }

  /** The record moved on since it was read, or a bed is taken. */
  get isConflict(): boolean {
    return this.status === 409
  }

  /** No reply at all: offline, or the server is down. */
  get isUnreachable(): boolean {
    return this.status === 0
  }
}

function detailCode(detail: unknown): string | null {
  if (detail && typeof detail === 'object') {
    const code = (detail as { error?: unknown }).error
    if (typeof code === 'string' && code.trim()) return code
  }
  return null
}

/** Whatever `apiFetch` (or `fetch`) threw, as an AdtApiError. */
export function toAdtError(err: unknown): AdtApiError {
  if (err instanceof AdtApiError) return err
  const raw = (err ?? {}) as {
    message?: unknown
    status?: unknown
    detail?: unknown
    permissions?: unknown
  }
  const status = typeof raw.status === 'number' ? raw.status : 0
  // apiFetch puts a 403's structured body on `permissions`, every other one on
  // `detail`.
  const detail = raw.detail ?? raw.permissions ?? null
  const code = detailCode(detail) ?? (status ? `http_${status}` : 'network_error')
  const message = status === 0
    ? UNREACHABLE_MESSAGE
    : (typeof raw.message === 'string' && raw.message.trim()
        ? raw.message
        : errorDetailMessage(detail, status))
  return new AdtApiError(message, status, code, detail)
}

export type AdtQueryValue = string | number | boolean | null | undefined

/** `?a=1&b=x` from a params object. Empty, null and undefined values are left
 *  out, so an unset filter is never sent as the string "undefined". */
export function adtQuery(params: Record<string, AdtQueryValue> = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

function areaPath(path: string): string {
  if (!path.startsWith('/')) {
    throw new Error(`beds and admissions paths start with "/" (got "${path}")`)
  }
  return `${ADT_API_PREFIX}${path}`
}

/** One call to /api/adt<path>. Throws AdtApiError. */
export async function adtRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const target = areaPath(path)
  try {
    return await apiFetch<T>(target, init)
  } catch (err) {
    throw toAdtError(err)
  }
}

export function adtGet<T>(path: string, params?: Record<string, AdtQueryValue>): Promise<T> {
  return adtRequest<T>(`${path}${adtQuery(params)}`)
}

/** A POST. Every ADT write takes an Idempotency-Key; pass one with `key`. */
export function adtPost<T>(path: string, body?: unknown, key?: string): Promise<T> {
  const init: RequestInit = { method: 'POST', body: JSON.stringify(body ?? {}) }
  if (key) init.headers = { 'Idempotency-Key': key }
  return adtRequest<T>(path, init)
}

/** What GET /api/adt/meta answers. No patient data. */
export interface AdtMeta {
  module?: string
  state?: 'on' | 'draining' | 'off'
  features?: Record<string, boolean>
  settings?: Record<string, unknown>
  [key: string]: unknown
}

/** The module's own probe. A 404 `module_disabled` means switched off here. */
export function getAdtMeta(): Promise<AdtMeta> {
  return adtGet<AdtMeta>('/meta')
}
